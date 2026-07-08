"""CostAgent 路由 —— ``POST /cost/compose``：构件 → 选码 → 组价取数。

流水线在 ``cost.orchestration.compose``（bill_match → select_code → price_compose）；本层只做
HTTP 编排 + 异常→状态码映射 + 可观测性 meta。选码红线（need_review / 不造码）在 selection 层，
组价取数缺口（no_source / 2013 未就绪）在 orchestration 层透传，本层原样冒泡到响应。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from common.config import COST_DEFAULT_REGION, COST_DEFAULT_SPEC
from cost import orchestration
from cost import gates
from cost.pricing import (
    HierarchyRollupInput,
    RollupInput,
    UnitPriceInput,
    compute_unit_price,
    rollup_cost,
    rollup_hierarchy,
)

logger = logging.getLogger("ce-services.cost")

router = APIRouter(tags=["cost-agent"])


class PricingRates(BaseModel):
    """组价费率块（可选）—— 给定则 ``/cost/compose`` 末步对每条定额算综合单价（确定性、不入 LLM）。

    字段：management_fee_rate / profit_rate —— 管理费率 / 利润率（%，库内无须 HITL 给定）；
      risk_rate —— 风险费率（%，默认 0）；fee_base —— 取费基数（labor / labor_machine / lmm，必填）；
      tax_rate —— 增值税率（%，可选）。缺该块时 ``/cost/compose`` 维持 P1 行为（仅选码 + 取数，不算钱）。
    """

    management_fee_rate: float = Field(..., description="管理费率 %")
    profit_rate: float = Field(..., description="利润率 %")
    risk_rate: float = Field(0.0, description="风险费率 %，默认 0")
    fee_base: str = Field(..., description="取费基数：labor / labor_machine / lmm")
    tax_rate: float | None = Field(None, description="增值税率 %，可选")


class CostComposeRequest(BaseModel):
    """CostAgent 组价请求体。

    字段：
        description —— 构件/做法自然语言描述。
        spec —— 国标版本（2013/2024）；**可缺省**：缺则归一到默认口径深圳·2013（PRD §4.0/C-05
        不反问，T9-1 行为反转；此前必填）。显式给定仍按版本严格隔离；知识层边界 spec 依旧必填，
        默认值由本任务层作为调用方补齐。
        region —— 地区（如「深圳」），用于信息价/定额取数。
        top_k —— 清单候选召回数。
        rates —— 可选费率块（``PricingRates``）；给定则末步算综合单价，否则维持 P1 仅选码+取数。
    """

    description: str = Field(..., description="构件/做法描述")
    spec: str | None = Field(None, description="国标版本 2013/2024；缺省默认深圳·2013（§4.0 不反问）")
    region: str = Field(COST_DEFAULT_REGION, description="地区，默认深圳")
    top_k: int = 10
    rates: PricingRates | None = Field(None, description="可选费率块，给定则算综合单价")


@router.post("/cost/compose")
def cost_compose_endpoint(req: CostComposeRequest) -> dict:
    """构件描述 → 候选召回 → LLM 选码 → 组价取数。

    参数：req —— CostComposeRequest（description / spec / region / top_k）。
    返回：``{description, spec, region, candidates_count, selection, code, price, price_status, meta}``；
      知识服务未知 spec→400 / 不可达→503、组价清单不存在→404、LLM 不可达→503 经 HTTPException 映射。
      选码 need_review 与 price_status（skipped / 未就绪 / ok）原样返回，由调用方据此走 HITL。
    """
    t0 = time.perf_counter()
    spec = req.spec or COST_DEFAULT_SPEC  # §4.0 口径归一：缺版本不反问，默认深圳·2013（T9-1）
    try:
        result = orchestration.compose(
            req.description, spec, req.region, top_k=req.top_k,
            rates=req.rates.model_dump() if req.rates else None,
            spec_source="user" if req.spec else "default",
        )
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=code, detail=f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"依赖服务不可达（知识服务 :8100 / LLM :8099）: {exc}") from exc
    except ValueError as exc:  # call_qwen3 的 json.JSONDecodeError（ValueError 子类）
        raise HTTPException(status_code=502, detail=f"LLM 选码输出非合法 JSON: {exc}") from exc

    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("/cost/compose spec=%s(%s) region=%s candidates=%d code=%s need_review=%s price_status=%s (%.0fms)",
                spec, "user" if req.spec else "default", req.region,
                result.get("candidates_count", 0), result.get("code"),
                result.get("selection", {}).get("need_review"), result.get("price_status"), elapsed_ms)
    # compose 已在 meta 里挂 guard（③ 校验闸）；这里 merge 计时/参数，勿 clobber。
    result.setdefault("meta", {}).update({"elapsed_ms": round(elapsed_ms), "top_k": req.top_k})
    return result


class FeatureSpec(BaseModel):
    """多构件条目（两级汇总分组）——描述 + 可选单项/单位工程归属。

    字段：feature —— 构件/做法描述（必填）；single_work / unit_work —— 所属单项工程 / 单位工程名
      （可选，缺则归默认组，退化单组树，见 `pricing.rollup_hierarchy`）。``features`` 元素可为纯字符串
      （无分组）或本结构（带分组）。
    """

    feature: str = Field(..., description="构件/做法描述")
    single_work: str | None = Field(None, description="所属单项工程名（可选）")
    unit_work: str | None = Field(None, description="所属单位工程名（可选）")


class SessionStartRequest(BaseModel):
    """HITL 组价会话起始请求体。

    字段：feature —— 构件/做法描述（必填）；spec —— 国标版本 2013/2024（缺则默认深圳·2013，
      §4.0 不反问；setup 节点留口径声明事件供审计与首答声明）；
      region —— 地区（默认深圳）；period —— 信息价期号；price_source —— 信息价来源（local/online/manual）；
      rates —— 可选费率块（给定则末步算综合单价）；quantity —— 可选工程量 Q（给定则 quantity_gate 自动过）；
      features —— 多构件列表（给定则逐件办，优先于 feature）；元素可为纯描述字符串或 ``FeatureSpec``（带两级汇总分组）。
    """

    feature: str | None = Field(None, description="单构件/做法描述（features 给定时可省）")
    features: list[str | FeatureSpec] | None = Field(
        None, description="多构件列表，逐件外层循环办，优先于 feature；元素可为字符串或带分组的 FeatureSpec")
    spec: str | None = Field(None, description="国标版本 2013/2024，缺省默认深圳·2013（§4.0 不反问）")
    region: str = Field("深圳", description="地区，默认深圳")
    period: str | None = Field(None, description="信息价期号（年月）")
    price_source: str | None = Field(None, description="信息价来源 local/online/manual")
    rates: dict | None = Field(None, description="可选费率块")
    quantity: float | None = Field(None, gt=0, description="可选工程量 Q（清单数量，>0，仅单构件预供），缺则 quantity_gate 录入")


class SessionResumeRequest(BaseModel):
    """HITL 会话恢复请求体 —— 闸的用户决策。

    字段：decision —— confirm 闸传 ``{action: approve|select_alternative|manual_override, value?}``；
      input 闸（setup/缺价录入）传字段值 dict（如 ``{value: 480}`` 或 ``{spec_version:"2024", ...}``）。
    """

    decision: dict = Field(..., description="confirm 动作或 input 字段值")


class SessionRewindRequest(BaseModel):
    """HITL 会话回退请求体 —— 回到某个已过的闸重答。

    字段：to_node —— 目标闸节点名（setup / feature_gate / list_gate / quota_gate / price_gate /
      quantity_gate / rates_gate / params_gate / rollup）；其后的已钉值随回退作废、重新确认。
    """

    to_node: str = Field(..., description="回退目标闸节点名")


def _map_session_exc(exc: Exception) -> HTTPException:
    """把会话推进时底层原语的异常映射成 HTTPException（沿用 compose 端点同款映射）。"""
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        return HTTPException(status_code=code, detail=f"依赖服务返回错误: {detail}")
    if isinstance(exc, requests.RequestException):
        return HTTPException(status_code=503, detail=f"依赖服务不可达（知识服务 :8100 / LLM :8099）: {exc}")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=502, detail=f"LLM 选码输出非合法 JSON: {exc}")
    raise exc


@router.post("/cost/session/start")
def cost_session_start_endpoint(req: SessionStartRequest) -> dict:
    """起一个可中断组价 HITL 会话，跑到首个闸或 done。

    参数：req —— ``SessionStartRequest``。返回：``{task_id, status, interrupt, events, items, overrides, audit_log}``；
      首个 interrupt 通常是编码确认闸（低置信/多候选时），或 spec 缺失时的 setup 录入闸；高置信可一路自动过。
      底层原语异常（知识服务 / LLM）按 compose 端点同款状态码映射。
    """
    from cost import session  # 懒加载：隔离 langgraph 依赖，不影响 /cost/compose 简单路径
    if not req.feature and not (req.features and any(f for f in req.features)):
        raise HTTPException(status_code=422, detail="需提供 feature（单构件）或 features（多构件，非空）")
    # FeatureSpec → 普通 dict（session 层不依赖 pydantic 模型），纯字符串原样透传。
    features = None
    if req.features:
        features = [f if isinstance(f, str) else f.model_dump() for f in req.features]
    try:
        result = session.start(req.feature, spec=req.spec, region=req.region,
                               period=req.period, price_source=req.price_source, rates=req.rates,
                               quantity=req.quantity, features=features)
    except (requests.RequestException, ValueError) as exc:
        raise _map_session_exc(exc) from exc
    logger.info("/cost/session/start task=%s status=%s feature=%s", result.get("task_id"),
                result.get("status"), req.feature)
    return result


@router.post("/cost/session/{task_id}/resume")
def cost_session_resume_endpoint(task_id: str, req: SessionResumeRequest) -> dict:
    """以用户决策续跑会话到下个闸或 done。

    参数：task_id —— 会话标识；req.decision —— 闸的用户输入。
    返回：会话响应（下个 interrupt 或终态）；未知 task_id 由 langgraph 视为新线程、状态空（next 为 START）。
    """
    from cost import session
    try:
        result = session.resume(task_id, req.decision)
    except (requests.RequestException, ValueError) as exc:
        raise _map_session_exc(exc) from exc
    logger.info("/cost/session/%s/resume status=%s", task_id, result.get("status"))
    return result


def _sse(messages: Iterator[dict]) -> Iterator[str]:
    """把会话流式消息逐条编成 SSE 帧（``data: {json}\\n\\n``）。生成器内异常已由 session 层转 error 消息。"""
    for msg in messages:
        yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"


# SSE 响应头：关缓存 + 关 nginx 代理缓冲（X-Accel-Buffering），保逐条即时下发。
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/cost/session/start/stream")
def cost_session_start_stream_endpoint(req: SessionStartRequest) -> StreamingResponse:
    """起 HITL 会话并**逐节点 SSE 流式**到首个闸或终态（``/cost/session/start`` 的流式版）。

    参数：req —— ``SessionStartRequest``（同非流式）。返回：``text/event-stream``，逐条
      ``{type:start|event|interrupt|done|error,...}``（见 session.stream_start / _run_stream）。
      入参校验失败（无 feature/features）→ 422；节点内依赖/LLM 异常在流内转 error 事件（不中断连接）。
    """
    from cost import session
    if not req.feature and not (req.features and any(f for f in req.features)):
        raise HTTPException(status_code=422, detail="需提供 feature（单构件）或 features（多构件，非空）")
    features = None
    if req.features:
        features = [f if isinstance(f, str) else f.model_dump() for f in req.features]
    gen = session.stream_start(req.feature, spec=req.spec, region=req.region,
                               period=req.period, price_source=req.price_source, rates=req.rates,
                               quantity=req.quantity, features=features)
    logger.info("/cost/session/start/stream feature=%s", req.feature)
    return StreamingResponse(_sse(gen), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/cost/session/{task_id}/resume/stream")
def cost_session_resume_stream_endpoint(task_id: str, req: SessionResumeRequest) -> StreamingResponse:
    """以用户决策**逐节点 SSE 流式**续跑会话到下个闸或终态（``/cost/session/{id}/resume`` 的流式版）。

    参数：task_id；req.decision —— 闸决策。返回：``text/event-stream``，逐条步骤事件 / 暂停闸 / 终态 / error。
    """
    from cost import session
    logger.info("/cost/session/%s/resume/stream", task_id)
    return StreamingResponse(_sse(session.stream_resume(task_id, req.decision)),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/cost/session/{task_id}/rewind")
def cost_session_rewind_endpoint(task_id: str, req: SessionRewindRequest) -> dict:
    """回退会话到某个已过的闸重答（丢弃其后已钉值），重新停在该闸。

    参数：task_id —— 会话标识；req.to_node —— 目标闸节点名。
    返回：会话响应（重新停在该闸的 interrupt）；to_node 非法/未到达 → status=error（业务错误，非 HTTP 错误）。
      底层 langgraph 时间旅行（``get_state_history`` + 从历史 checkpoint 重 invoke）；上游 compute 不重跑。
    """
    from cost import session
    try:
        result = session.rewind(task_id, req.to_node)
    except (requests.RequestException, ValueError) as exc:
        raise _map_session_exc(exc) from exc
    logger.info("/cost/session/%s/rewind to=%s status=%s", task_id, req.to_node, result.get("status"))
    return result


class SessionReRollupRequest(BaseModel):
    """会话总造价 what-if 重算请求 —— 改费率/项目费用后重算（不改会话，红线7 的确定性重算通道）。

    字段：params_override —— 项目费用/税率覆盖（measure_fee/other_fee/fee_levy/tax_rate，仅重算顶层）；
      rates_override —— 费率覆盖（management_fee_rate/profit_rate/risk_rate/fee_base，变则逐件重算综合单价）。
      两者皆可空（都空=按现值重算，等价于读一次总造价）。
    """

    params_override: dict | None = Field(None, description="项目费用/税率覆盖 measure_fee/other_fee/fee_levy/tax_rate")
    rates_override: dict | None = Field(None, description="费率覆盖 management_fee_rate/profit_rate/risk_rate/fee_base")


@router.post("/cost/session/{task_id}/re_rollup")
def cost_session_re_rollup_endpoint(task_id: str, req: SessionReRollupRequest) -> dict:
    """读会话累积态 + 套修正费率/项目费用 → 确定性重算总造价（不改会话、不重跑 LLM）。

    参数：task_id —— 已完成/进行中的会话；req —— 费率/项目费用覆盖。
    返回：``{task_id, status:"recomputed", rollup, rates, params}``；会话无构件数据 → status="error"。
      用途：已出总价后改税率/费率要新总价——重算走服务端确定性图，避免弱模型对话内凭文本重算（红线7）。
    """
    from cost import session
    result = session.re_rollup(task_id, params_override=req.params_override, rates_override=req.rates_override)
    logger.info("/cost/session/%s/re_rollup status=%s", task_id, result.get("status"))
    return result


@router.get("/cost/session/{task_id}/state")
def cost_session_state_endpoint(task_id: str) -> dict:
    """读会话当前持久化状态（不推进图）。

    参数：task_id。返回：``{task_id, status, next, values}``——values 为完整 §5.4 状态文档；
      task_id 不存在 → status="unknown"、values 空（langgraph 无该线程 checkpoint）。
    """
    from cost import session
    return session.get_state(task_id)


class BatchDecision(BaseModel):
    """批量续跑的单条决策（(node, item) 寻址）。"""

    node: str = Field(..., description="闸节点名（interrupt payload 的 node：list_coding/quota/price_item:N/quantity/rates/params/rollup…）")
    item: int | None = Field(None, description="构件序号（省略=不限件；项目级闸无意义）")
    decision: dict = Field(default_factory=dict, description="该闸的用户输入（与单闸 resume 同形）")


class BatchResumeRequest(BaseModel):
    """POST /cost/session/{id}/batch_resume 请求体。"""

    decisions: list[BatchDecision] = Field(..., min_length=1, max_length=500,
                                           description="决策池：命中即消耗；未命中的闸停下留人工")


@router.post("/cost/session/{task_id}/batch_resume")
def cost_session_batch_resume_endpoint(task_id: str, req: BatchResumeRequest) -> dict:
    """批量决策续跑（M2 评审表后端）：一次提交多闸决策，服务端循环 resume 到池尽/未命中闸/终态。

    评审表语义：高置信件的确认批量提交、低置信件不给决策自然停闸留人工。
    返回：会话响应 + ``batch{consumed, steps, stopped_reason, remaining_decisions}``。
    """
    from cost import session
    result = session.batch_resume(task_id, [d.model_dump() for d in req.decisions])
    logger.info("/cost/session/%s/batch_resume steps=%d stopped=%s", task_id,
                result["batch"]["steps"], result["batch"]["stopped_reason"])
    return result


@router.get("/cost/session/{task_id}/export")
def cost_session_export_endpoint(task_id: str, fmt: str = "xlsx"):
    """导出会话组价成果为清单-定额-单价表（M2 §3.1 管线⑦，只读不推进图）。

    参数：task_id；fmt —— ``xlsx``（缺省）/ ``csv``。
    返回：文件下载（Content-Disposition attachment）。逐行带 provenance（编码来源/置信/确认方式）、
      汇总区透传 rollup 确定性结果，缺口如实标注不补数。会话无构件 → 404；xlsx 依赖未装 → 501
      （提示用 csv 或服务器 ``uv add openpyxl``）。
    """
    from fastapi.responses import Response

    from cost import export, session
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail=f"不支持的导出格式 {fmt!r}（xlsx / csv）")
    values = session.get_state(task_id).get("values") or {}
    if not values.get("items"):
        raise HTTPException(status_code=404, detail=f"会话 {task_id} 无构件数据，无可导出内容")
    rows = export.build_rows(values)
    if fmt == "csv":
        body, media = export.to_csv_bytes(rows), "text/csv; charset=utf-8"
    else:
        try:
            body = export.to_xlsx_bytes(rows)
        except ImportError as exc:
            raise HTTPException(status_code=501,
                                detail=f"xlsx 依赖未安装（uv add openpyxl），可先用 fmt=csv：{exc}") from exc
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    filename = f"cost-{task_id[:8]}.{fmt}"
    logger.info("/cost/session/%s/export fmt=%s items=%d", task_id, fmt, len(values.get("items") or []))
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class BoqRow(BaseModel):
    """一条 BOQ 清单行（FR-C 核对输入）。除 code 外均可选——给什么核什么。"""

    code: str = Field(..., description="清单编码（9 位全国码或 12 位含顺序码）")
    name: str | None = Field(None, description="清单项名称")
    unit: str | None = Field(None, description="计量单位")
    quantity: float | None = Field(None, description="工程量")
    unit_price: float | None = Field(None, description="综合单价（元）")
    amount: float | None = Field(None, description="合价（元）")


class CostCheckRequest(BaseModel):
    """FR-C 清单核对请求体。

    字段：rows —— BOQ 行列表（1–500）；spec —— 国标版本（缺省默认深圳·2013，§4.0 不反问）；
      region —— 地区（声明用，默认深圳）。BOQ 行随请求进出、服务端不落盘（多租户边界在 deer-flow 层）。
    """

    rows: list[BoqRow] = Field(..., min_length=1, max_length=500, description="BOQ 清单行")
    spec: str | None = Field(None, description="国标版本 2013/2024，缺省默认深圳·2013")
    region: str = Field(COST_DEFAULT_REGION, description="地区，默认深圳")


@router.post("/cost/check")
def cost_check_endpoint(req: CostCheckRequest) -> dict:
    """FR-C 项目上下文核对 v1：BOQ 清单行确定性核对（编码有效性/单位一致性/名称偏离/合价算术）。

    参数：req —— ``CostCheckRequest``。返回：``{spec, region, total, rows_with_issues,
      issues:[{row_index, code, checks:[{type,severity,detail}], bill}], unsupported, meta}``；
      漏项/高估冒算/单价偏差 v1 诚实列入 unsupported（宁缺毋造）。知识服务不可达→503、未知 spec→400。
    """
    from cost import check as boq_check  # 轻模块，与 compose 依赖隔离
    try:
        result = boq_check.check_boq([r.model_dump() for r in req.rows],
                                     spec=req.spec, region=req.region)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=code, detail=f"知识服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"ce-db 不可达（:8102 /bill/{{code}}）: {exc}") from exc
    logger.info("/cost/check spec=%s rows=%d issues=%d",
                result.get("spec"), result.get("total"), result.get("rows_with_issues"))
    return result


@router.post("/cost/unit-price")
def cost_unit_price_endpoint(req: UnitPriceInput) -> dict:
    """综合单价计算原语（P2 ``compute_unit_price`` 的 HTTP 表面）——人材机费 + 费率 → 综合单价 →（可选）含税。

    参数：req —— ``UnitPriceInput``；FastAPI 据其 pydantic schema 自动校验，非法入参（负数 / NaN/Inf /
      缺取费基数 / 多余字段）直接 422——这就是「算钱那步的 pydantic 闸门」，无论谁/哪条路径调用都被拦在边界。
    返回：``compute_unit_price`` 结果（综合单价 + 六项构成 + 综合合价 + 可选含税 + 溯源 + 红线声明）。
      纯确定性、不入 LLM；费率由调用方给定，本端点不杜撰、不内置地区默认。
    """
    return compute_unit_price(req)


@router.post("/cost/rollup")
def cost_rollup_endpoint(req: RollupInput) -> dict:
    """总造价汇总原语（§13 ``rollup_cost`` 的 HTTP 表面）——分部分项 + 措施/其他/规费 →（可选税金）总造价。

    参数：req —— ``RollupInput``；FastAPI 据 pydantic schema 自动校验，负金额/NaN/Inf/多余字段 → 422 闸门。
    返回：``rollup_cost`` 结果（税前造价 + 可选税金/总造价 + 溯源 + 红线声明）。纯确定性、不入 LLM；
      税率与项目级费用由调用方给定，本端点不杜撰、不内置默认（不替用户定政策数）。
    """
    return rollup_cost(req)


@router.post("/cost/rollup-hierarchy")
def cost_rollup_hierarchy_endpoint(req: HierarchyRollupInput) -> dict:
    """层级总造价汇总原语 —— 构件行 → 单位工程 → 单项工程 → 项目总造价。

    参数：req —— ``HierarchyRollupInput``；items[].total_price=None 表示该构件未计价，逐层计入
      ``missing_unit_price_items``，不计金额、不虚构。
    返回：``rollup_hierarchy`` 结果（两级明细 + 项目级费用/税金/总价）。纯确定性、不入 LLM。
    """
    return rollup_hierarchy(req)


class GateDecisionRequest(BaseModel):
    """门控判断请求体 —— 暴露 ``gates.should_pause_*`` 的 HTTP 表面。

    gate 取值：
      coding / quota / price / quantity / rates / params / basis_complete / has_priceable_item。
    payload 按 gate 传对应字段：
      coding={env,tau?}; quota={env}; price={price}; quantity={quantity}; rates={rates}; params={params};
      basis_complete={basis}; has_priceable_item={items}。
    """

    gate: str = Field(..., description="门控类型")
    payload: dict = Field(default_factory=dict, description="门控输入数据")


@router.post("/cost/gate-decision")
def cost_gate_decision_endpoint(req: GateDecisionRequest) -> dict:
    """组价 HITL 门控判断原语 —— 当前步骤数据 → 是否需要人工介入。

    返回：``{gate, needs_human_input, reason, payload?}``；coding/quota 需要人工时会附 confirm payload。
    本端点只判断，不推进 session、不落值、不调用 LLM。
    """
    gate, payload = req.gate, req.payload
    try:
        if gate == "coding":
            pause = gates.should_pause_coding(payload["env"], float(payload.get("tau", 0.75)))
            out = {"needs_human_input": pause, "reason": "清单编码低置信/多备选/需复核" if pause else "清单编码可自动通过"}
            if pause:
                out["payload"] = gates.confirm_payload("list_coding", payload["env"], "请确认清单编码")
            return {"gate": gate, **out}
        if gate == "quota":
            pause = gates.should_pause_quota(payload["env"])
            out = {"needs_human_input": pause, "reason": "无定额或多定额子目需确认" if pause else "唯一子目可自动通过"}
            if pause:
                out["payload"] = gates.confirm_payload("quota", payload["env"], "请确认套用定额子目")
            return {"gate": gate, **out}
        if gate == "price":
            pause = gates.should_pause_price(payload["price"])
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "信息价缺失或命中无值" if pause else "信息价可自动通过"}
        if gate == "quantity":
            pause = gates.should_pause_quantity(payload.get("quantity"))
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "工程量 Q 缺失，需人工录入" if pause else "工程量已给定"}
        if gate == "rates":
            pause = gates.should_pause_rates(payload.get("rates"))
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "管理费率/利润率/取费基数缺失" if pause else "综合单价费率已齐"}
        if gate == "params":
            pause = gates.should_pause_params(payload.get("params"))
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "税金率缺失，需人工录入" if pause else "项目级费用参数已齐"}
        if gate == "basis_complete":
            ok = gates.basis_complete(payload.get("basis"))
            return {"gate": gate, "needs_human_input": not ok,
                    "reason": "人材机基价三项未齐" if not ok else "人材机基价完整"}
        if gate == "has_priceable_item":
            ok = gates.has_priceable_item(payload.get("items") or [])
            return {"gate": gate, "needs_human_input": not ok,
                    "reason": "全单无可算价构件" if not ok else "至少一项构件可算价"}
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"门控 {gate!r} 缺少必填 payload 字段: {exc}") from exc
    raise HTTPException(status_code=422, detail=f"未知门控类型 gate={gate!r}")


class ManualQuotaBasisRequest(BaseModel):
    """人工补录定额基价规范化请求体。"""

    quota_code: str | None = Field(None, description="定额子目号（可选）")
    labor_cost: float | None = Field(None, ge=0, description="定额人工费基价")
    material_cost: float | None = Field(None, ge=0, description="定额材料费基价")
    machine_cost: float | None = Field(None, ge=0, description="定额机械费基价")


@router.post("/cost/manual-quota-basis")
def cost_manual_quota_basis_endpoint(req: ManualQuotaBasisRequest) -> dict:
    """人工补录定额基价规范化 —— 三项全齐才生成 quota_basis，半填要求继续补齐。

    返回：``{basis, needs_human_input, reason}``。三项全空表示放弃补录；半填不静默丢弃。
    """
    data = req.model_dump()
    if gates.has_partial_costs(data):
        return {"basis": None, "needs_human_input": True,
                "reason": "人材机三项需一起填齐，或三项全空表示放弃补录"}
    basis = gates.build_manual_basis(data)
    if basis is None:
        return {"basis": None, "needs_human_input": False, "reason": "用户未补录定额基价，跳过该构件"}
    return {"basis": basis, "needs_human_input": False, "reason": "人工补录定额基价完整"}
