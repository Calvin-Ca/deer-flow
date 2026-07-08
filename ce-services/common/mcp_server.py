"""任务层能力 MCP façade（FastMCP streamable-HTTP，任务服务 :8101 `/mcp`）。

在现有 HTTP REST（``/norm/qa`` / ``/cost/compose``）之外**加一层讲 MCP 协议的 façade**，让任意
deer-flow agent 把两个**无状态、一把出结果**的任务层能力当 tool 直接调，且工具名/入参/结果天然
结构化——前端 toolCall 流据此按工具名渲染领域结果（cited_clauses / 选码+置信度），不再 sniff
bash 命令、不再把结论埋在 bash stdout 里看不见。

与知识层 façade 分工：
  - ``ce-rag``（默认 :8100）—— **证据/候选原语**：search_clause / match_bill_item / search_price_rule …；
  - ``ce-db``（默认 :8102）—— **纯数据原语**：bill_get / quota_get / price_query / price_compose …；
  - ``ce-task``（本文件，:8101）—— **带 LLM 编排的任务层能力**：orchestrate（四层骨架前门：
    确定性路由 → 单一直派 / 复合拆解-综合，§9 T-A4）、norm_qa（检索+带引用作答）、
    cost_compose（候选召回 → LLM 选码 → 组价取数）、start_cost_session（起可中断 HITL 完整组价会话，只点火）。

设计要点（与知识层 façade 一致）：
  - **复用同一份编排内核**：两个 ``@mcp.tool`` 直接调 ``knowledge_client.search`` + ``generation.answer``
    与 ``orchestration.compose``，**不反代自家 REST**（不再起一跳 HTTP 打 :8101 自己）。
  - **红线下沉到工具边界**：``spec``/``standard`` 必填无默认（逼调用方选国标版本，杜绝 2013/2024
    串库）；零召回不喂空上下文给 LLM 编答案（norm_qa 直返「无依据」）；选不出码 need_review、缺价
    no_source、2013 未就绪等如实透传（compose 内核已保证），不杜撰。
  - **无状态**（``stateless_http=True``）：每请求一新 transport。HITL 可中断组价会话经 ``start_cost_session``
    **只暴露「点火」**（起会话、返回 ``cost-hitl`` marker），逐闸交互（有状态）仍由前端内嵌卡片驱动 REST
    ``/cost/session/*``——编排在服务端图里，MCP 工具不当编排器（红线，见 HITL_DESIGN §10）。会话状态在
    SqliteSaver 按 task_id 持久化，stateless transport 无碍。

工具名前缀：deer-flow 加载 MCP 工具时按 server 名加前缀（``ce-task`` → agent 见 ``ce-task_norm_qa``
``ce-task_cost_compose``）。

注册（``extensions_config.json`` 的 ``mcpServers``）::

    "ce-task": {
      "enabled": true,
      "type": "http",
      "url": "http://localhost:8101/mcp",
      "description": "深圳房建任务层能力：norm_qa（规范问答）/ cost_compose（选码+组价取数）/ start_cost_session（起 HITL 完整组价会话）"
    }

挂载/启动：
  - 正式：随 ``main.py`` 一起对外 :8101，挂在 ``/mcp``（见 main.py ``app.mount`` + lifespan）。
  - 独立单跑（调试）：``cd ce-services && uv run python -m common.mcp_server``（默认 :8101，仅 MCP）。
"""
from __future__ import annotations

import json
from typing import Annotated

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

try:  # ToolError 在不同 mcp 版本路径略有差异，缺则退化为 ValueError（FastMCP 同样会转 isError）
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError:  # pragma: no cover
    ToolError = ValueError  # type: ignore[assignment,misc]

from common.config import COST_DEFAULT_SPEC, LLM_MODEL_ID, LLM_URL
from cost import orchestration
from norm import pipeline
from routing.orchestrator import orchestrate as _orchestrate

# 内网可信服务，关掉 DNS rebinding 保护——允许经 localhost / 服务器 IP 任意 Host 访问 :8101/mcp。
mcp = FastMCP(
    "ce-task",
    instructions=(
        "深圳房建任务层能力（按国标版本严格隔离 2013/2024）：\n"
        "  · orchestrate —— 【前门】原始请求 → 确定性路由 → 单一直派 / 复合拆解-综合；不确定问谁、"
        "或一句话含多诉求时用它，自动分流（内部调下面两能力）\n"
        "  · norm_qa —— 造价/计量/计价规范问题 → 检索条文 + 带引用结构化作答（零召回拒答，不编造条文）\n"
        "  · cost_compose —— 构件描述 → 清单候选召回 → LLM 选码 → 组价取数（缺价标 no_source、"
        "选不出码转 HITL，绝不杜撰）\n"
        "  · quota_lookup —— 【已知清单码】直查套定额取数（纯键查、不选码；从描述选码必走 cost_compose）\n"
        "  · price_lookup —— 【材料/人工/机械名】直查当期信息价（纯键查，零命中诚实返回）\n"
        "  · start_cost_session —— 起可中断 HITL 完整组价会话（走到总造价、逐闸确认/录入）：只点火、"
        "返回 cost-hitl marker 供前端内嵌控件驱动，你**不逐闸编排**"
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def norm_qa(
    query: Annotated[str, Field(description="造价/计量/计价类自然语言问题，如「现浇混凝土柱按什么计量」")],
    standard: Annotated[
        str | None,
        Field(description="规范代号 hint（可选）：如 gb50854-2024。服务端按问题类型确定性定族"
                          "（计量→50854/计价→50500/安装→50856），仅零命中时回退此 hint，错族由确定性夺回"),
    ] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="检索召回条数")] = 15,
    skip_rerank: Annotated[bool, Field(description="跳过 cross-encoder 精排（调试用）")] = False,
) -> dict:
    """造价规范条文检索 + Qwen3 带引用作答（任务层 /norm/qa 的 MCP 表面）。

    **红线**：只引检索到的条文、零召回不喂空上下文给 LLM（直返「未检索到」），不编造条文号/原文。
    **规范选择确定化（T-A2）**：选哪部规范由服务端 ``standard_router`` 按问题类型确定性裁定，
    不再由（弱模型）调用方说了算——治「计量问题被路由到计价规范」的标准漂移。

    参数：
        query —— 造价/计量/计价自然语言问题。
        standard —— 规范代号 hint（可选）：gb50854-2024 等；仅在确定性零命中时作回退。
        top_k —— 检索召回条数（1–50）。
        skip_rerank —— 跳过精排（调试）。
    返回：``{answer, cited_clauses:[{clause,standard,text,relevance}...], uncertain_aspects,
        out_of_scope_warnings, meta:{standard,standard_resolution,retrieved,...}}``；零召回时
        answer 为「未检索到」、cited_clauses 空（这是正确行为，非错误）。
    异常：未知规范 / 索引未就绪 / 知识服务不可达 / LLM 不可达或输出非法 JSON → ToolError。
    """
    # 编排内核与 /norm/qa 端点、复合编排器共用 norm.pipeline.answer_query（含 T-A2 规范选择 +
    # 校验闸 C-01/02/03）。本工具仅把异常映射为 ToolError。
    try:
        return pipeline.answer_query(
            query, standard_hint=standard, llm_url=LLM_URL, model_id=LLM_MODEL_ID,
            top_k=top_k, skip_rerank=skip_rerank,
        )
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"知识服务检索失败: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"依赖服务不可达（ce-rag / LLM）: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 子类
        raise ToolError(f"LLM 输出非合法 JSON: {exc}") from exc


@mcp.tool()
def cost_compose(
    description: Annotated[str, Field(description="构件/做法的自然语言描述，如「C30 现浇钢筋混凝土矩形柱」")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则归一到默认口径深圳·2013"
                                                  "（PRD §4.0 不反问）；显式给定仍按版本严格隔离")] = None,
    region: Annotated[str, Field(description="地区（当前仅深圳），用于定额过滤与信息价取价")] = "深圳",
    top_k: Annotated[int, Field(ge=1, le=50, description="清单候选召回数")] = 10,
) -> dict:
    """构件描述 → 候选召回 → LLM 选码 → 组价取数（任务层 /cost/compose 的 MCP 表面，P1 选码闭环）。

    **红线**：选不出码（need_review / code=None）→ 不调组价、price_status="skipped(need_review)"，转 HITL；
    缺信息价的工料机 ``unit_price=None`` + ``price_status="no_source"``，绝不杜撰；2013 组价数据未就绪 →
    仅返回选码、price_status 标未就绪。**本工具不算综合单价/总造价**（费率/税率是政策数，走 HITL 录入）。
    **口径（§4.0/T9-1）**：spec 缺省默认深圳·2013、不反问；``meta.caliber`` 带口径声明
    （spec_source=default 时应在首答向用户显示「口径：深圳·2013」）。

    参数：
        description —— 构件/做法描述；spec —— 国标版本 2013 / 2024（缺省默认 2013）；
        region —— 地区（深圳）；top_k —— 候选召回数（1–50）。
    返回：``{description, spec, region, candidates_count, selection:{code,confidence,reason,
        need_review,alternatives}, code, price, price_status, meta:{guard, caliber}}``；price 为组价取数结果
        （定额 + 工料机含量 + 信息价单价），未就绪/选不出码时为 None。
    异常：未知 spec / 知识服务不可达 / 清单项不存在 / LLM 不可达或输出非法 JSON → ToolError。
    """
    try:
        return orchestration.compose(
            description, spec or COST_DEFAULT_SPEC, region, top_k=top_k,
            spec_source="user" if spec else "default",
        )
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"依赖服务不可达（ce-rag / ce-db / LLM :8099）: {exc}") from exc
    except ValueError as exc:  # call_qwen3 的 json.JSONDecodeError
        raise ToolError(f"LLM 选码输出非合法 JSON: {exc}") from exc


@mcp.tool()
def quota_lookup(
    code: Annotated[str, Field(description="已知清单编码（9 位），如「010502001」")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则默认深圳·2013（§4.0 不反问）")] = None,
    region: Annotated[str, Field(description="地区（当前仅深圳），用于定额过滤与信息价取价")] = "深圳",
) -> dict:
    """**已知清单编码** → 套定额取数（定额子目 + 工料机含量 + 信息价单价）：纯键查、**不选码**。

    **何时用**：用户**直接给了清单码**、只想查「这码套什么定额 / 工料机取数」——键查场景。
    **与 ``cost_compose`` 分工（红线）**：从**构件描述**选码是判断题、有红线兜底，必须走 ``cost_compose``；
    本工具**不做任何选码判断**，只对给定 code 确定性取数。若 code 有歧义/不确定，别用本工具猜，转 ``cost_compose``。

    参数：code —— 已知清单编码；spec —— 国标版本 2013/2024（缺省默认深圳·2013）；region —— 地区（深圳）。
    返回：``{code, spec, region, price, caliber}``——price 为组价取数结果（定额工料机含量 + 信息价单价 + 小计，
      未命中信息价的资源 price_status=no_source，不杜撰）；caliber 为口径声明（spec_source=default 时首答应显示）。
    异常：spec 非 2013/2024 / 该版本组价数据未就绪（501）/ 清单项不存在（404）/ 知识服务不可达（503）→ ToolError。
    """
    _spec = spec or COST_DEFAULT_SPEC
    if _spec not in ("2013", "2024"):
        raise ToolError(f"未知国标版本 spec={spec!r}（仅支持 2013 / 2024，同码不同义不可混）")
    from common import cost_client
    try:
        price = cost_client.price_compose(region, code, _spec)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"ce-db 不可达（/price/compose）: {exc}") from exc
    caliber = {"declared": f"{region}·{_spec}", "region": region, "spec": _spec,
               "spec_source": "user" if spec else "default"}
    return {"code": code, "spec": _spec, "region": region, "price": price, "caliber": caliber}


@mcp.tool()
def price_lookup(
    name: Annotated[str, Field(description="材料/人工/机械名称（模糊匹配），如「商品混凝土」「螺纹钢」")],
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
    period: Annotated[str | None, Field(description="期号 YYYY-MM（可缺省，缺则各资源取最新期）")] = None,
    category: Annotated[str | None, Field(description="类别过滤：人工 / 材料 / 机械（可选）")] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="返回行数")] = 10,
) -> dict:
    """**材料/人工/机械名称** → 当期信息价：纯键查（动态价格数据，与国标版本无关）。

    **何时用**：用户直接问「某材料/人工/机械的信息价（当期价）多少」。零命中如实返回 count=0（诚实 no_source，非错误）。
    参数：name —— 名称（模糊）；region —— 地区（深圳）；period —— 期号 YYYY-MM（缺省最新）；
      category —— 人工/材料/机械 过滤；top_k —— 行数。
    返回：``{name, region, period, count, results:[{name,spec,unit,category,price,price_type,period_start,period_end,doc_id}...]}``。
    异常：PG/知识服务不可达（503）→ ToolError。
    """
    from common import cost_client
    try:
        return cost_client.price_query(name, region=region, period=period, category=category, top_k=top_k)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"ce-db 不可达（/price/query）: {exc}") from exc


@mcp.tool()
def start_cost_session(
    feature: Annotated[str, Field(description="构件/做法描述，如「C30 现浇钢筋混凝土矩形柱」")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则默认口径深圳·2013"
                                                  "（§4.0 不反问）；2013 组价数据未就绪时会话仍起、走到取数处如实标未就绪")] = None,
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
) -> dict:
    """起一个**可中断 HITL 完整组价会话**（走到总造价），只点火、不逐闸编排（任务层 session.start 的 MCP 表面）。

    **何时用**：用户要「走完整组价流程、算到总造价、要人逐步确认编码/定额/录入费率」——与 ``cost_compose``
    （一把出、只到选码+取数、不算钱）分工。**你（agent）只做一件事**：调本工具起会话，把返回的 ``marker``
    （```cost-hitl 代码块）**一字不改**贴进回复——前端识别后内嵌渲染交互式组价控件，用户在控件里逐闸点选/录入，
    控件直接驱动会话（走 REST /cost/session/*），**全程不再经过你**。这是红线：组价 13 步编排在服务端的图里，
    弱模型不当编排器（不逐闸 resume、不替用户做闸内决策、不跳闸）。

    参数：feature —— 构件/做法描述；spec —— 国标版本 2013/2024（缺省默认深圳·2013）；region —— 地区（深圳）。
    返回：``{task_id, marker, status, first_gate}``——marker 为须原样转贴的 cost-hitl 代码块；
      first_gate 为首个 interrupt 闸 payload（供参考，实际交互交前端控件）；status 为会话状态。
    异常：spec 非 2013/2024 → ToolError；知识服务/LLM 不可达或输出非法 JSON → ToolError（如实透传，不杜撰）。
    """
    if spec is not None and spec not in ("2013", "2024"):
        raise ToolError(f"未知国标版本 spec={spec!r}（仅支持 2013 / 2024，同码不同义不可混）")
    from cost import session  # 懒加载：隔离 langgraph 依赖，与 router 一致
    try:
        result = session.start(feature, spec=spec, region=region)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"依赖服务不可达（ce-rag / ce-db / LLM :8099）: {exc}") from exc
    except ValueError as exc:  # LLM 选码输出非法 JSON
        raise ToolError(f"LLM 选码输出非合法 JSON: {exc}") from exc
    task_id = result.get("task_id")
    marker = "```cost-hitl\n" + json.dumps({"task_id": task_id}, ensure_ascii=False) + "\n```"
    return {"task_id": task_id, "marker": marker,
            "status": result.get("status"), "first_gate": result.get("interrupt")}


@mcp.tool()
def orchestrate(
    query: Annotated[str, Field(description="用户请求（可复合，如「这做法套什么清单码，再解释它按什么计量、可否更省」）")],
    has_project_context: Annotated[
        bool | None,
        Field(description="是否已挂 BOQ/算量上下文（覆盖文本推断）；None 则由文本推断"),
    ] = None,
) -> dict:
    """四层骨架**前门**：确定性前置路由 → 单一直派能力层 / 复合 32b 拆解-综合。

    lead-agent 把**原始请求**整条交给本工具即可，无需自己判能力/拆子任务：
      - ① 前置路由（无 LLM）定 capability（组价/规范/价格/复合）+ 形态；
      - 单一意图 → 直派能力层 ②（norm_qa 检索带引用 / cost 选码+组价取数），过 ③ 校验闸（meta.guard）；
      - 复合意图（EH-01）→ 32b 拆解 → 每子任务回 ① 路由 → 派发 → 32b 综合（降级安全：LLM 挂则确定性拼接）。

    **何时用哪个**：模糊/复合/不确定该问谁 → 本工具（前门，自动分流）；已确定要「检索规范条文」或
    「构件→清单码→价」的单一能力，可直接调 ``ce-task_norm_qa`` / ``ce-task_cost_compose`` 原语（非 chokepoint）。
    HITL 完整组价（逐闸确认/算总造价）不走本工具——那是有状态交互，走 cost-agent 的 start + 内嵌卡。

    **红线（下沉能力层，不靠本层）**：规范零召回拒答不编条文、组价选不出码转人工、缺价 no_source 不杜撰；
    每子结果带 ``meta.guard``（C-01 溯源 / C-02 口径纯净 / C-03 拒答，norm+cost 同契约）。

    参数：
        query —— 用户自然语言请求（可复合）。
        has_project_context —— 是否已挂 BOQ/算量；None 由文本推断。
    返回：
        单一 ``{mode:"single", route, result:<能力层信封，含 meta.guard>}``；
        复合 ``{mode:"compound", route, subtasks:[{subtask, route, result}...], answer:<综合，含 cited_clauses>}``。
    异常：能力层依赖（ce-rag / ce-db / LLM）整体不可达且未被内部降级吸收 → ToolError。
    """
    try:
        return _orchestrate(query, has_project_context=has_project_context)
    except requests.RequestException as exc:
        raise ToolError(f"依赖服务不可达（ce-rag / ce-db / LLM）: {exc}") from exc
    except ValueError as exc:  # 拆解/综合 LLM 输出非法 JSON（多已内部降级，兜底映射）
        raise ToolError(f"编排 LLM 输出非合法 JSON: {exc}") from exc


if __name__ == "__main__":
    # 独立单跑（调试）：仅 MCP 端点，streamable-HTTP，默认挂 settings.streamable_http_path (/mcp)。
    # 正式随 main.py 一起 :8101（见该文件 app.mount("/", ...) + lifespan）。
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8101
    mcp.run(transport="streamable-http")
