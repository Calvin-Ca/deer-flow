"""任务层能力 MCP façade（FastMCP streamable-HTTP，任务服务 :8101 `/mcp`）。

在现有 HTTP REST（``/norm/qa`` / ``/cost/compose``）之外**加一层讲 MCP 协议的 façade**，让任意
deer-flow agent 把两个**无状态、一把出结果**的任务层能力当 tool 直接调，且工具名/入参/结果天然
结构化——前端 toolCall 流据此按工具名渲染领域结果（cited_clauses / 选码+置信度），不再 sniff
bash 命令、不再把结论埋在 bash stdout 里看不见。

与知识层 façade 分工：
  - ``ce-rag``（默认 :8100）—— **证据/候选原语**：search_clause / match_bill_item / search_price_rule …；
  - ``ce-db``（默认 :8102）—— **纯数据原语**：bill_get / quota_get / price_query / price_compose …；
  - ``ce-task``（本文件，:8101）—— **带 LLM 编排的任务层能力**：orchestrate_tool（四层骨架前门：
    确定性路由 → 单一直派 / 复合拆解-综合，§9 T-A4）、norm_qa_tool（检索+带引用作答）、
    cost_compose_tool（候选召回 → LLM 选码 → 组价取数）、start_cost_session_tool（起可中断 HITL 完整组价会话，只点火）。

设计要点（与知识层 façade 一致）：
  - **复用同一份编排内核**：两个 ``@mcp.tool`` 直接调 ``knowledge_client.search`` + ``generation.answer``
    与 ``orchestration.compose``，**不反代自家 REST**（不再起一跳 HTTP 打 :8101 自己）。
  - **红线下沉到工具边界**：``spec``/``standard`` 必填无默认（逼调用方选国标版本，杜绝 2013/2024
    串库）；零召回不喂空上下文给 LLM 编答案（norm_qa_tool 直返「无依据」）；选不出码 need_review、缺价
    no_source、2013 未就绪等如实透传（compose 内核已保证），不杜撰。
  - **无状态**（``stateless_http=True``）：每请求一新 transport。HITL 可中断组价会话经 ``start_cost_session_tool``
    **只暴露「点火」**（返回结构化 ``task_id`` / ``interrupt`` / ``ui_hint``），逐闸交互（有状态）仍由前端内嵌
    卡片驱动 REST ``/cost/session/*``——编排在服务端图里，MCP 工具不当编排器（红线，见 HITL_DESIGN §10）。
    会话状态在 SqliteSaver 按 task_id 持久化，stateless transport 无碍。

工具名前缀：deer-flow 加载 MCP 工具时按 server 名加前缀（``ce-task`` → agent 见 ``ce-task_norm_qa_tool``
``ce-task_cost_compose_tool``）。

注册（``extensions_config.json`` 的 ``mcpServers``）::

    "ce-task": {
      "enabled": true,
      "type": "http",
      "url": "http://localhost:8101/mcp",
      "description": "深圳房建任务层能力：norm_qa_tool（规范问答）/ cost_compose_tool（选码+组价取数）/ start_cost_session_tool（起 HITL 完整组价会话）"
    }

挂载/启动：
  - 正式：随 ``main.py`` 一起对外 :8101，挂在 ``/mcp``（见 main.py ``app.mount`` + lifespan）。
  - 独立单跑（调试）：``cd ce-services && uv run python -m common.mcp_server``（默认 :8101，仅 MCP）。
"""
from __future__ import annotations

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
from cost.tools import (
    cost_build_manual_quota_basis_tool as _cost_build_manual_quota_basis_tool,
    cost_compute_unit_price_tool as _cost_compute_unit_price_tool,
    cost_gate_decision_tool as _cost_gate_decision_tool,
    cost_match_bill_item_tool as _cost_match_bill_item_tool,
    cost_price_compose_envelope_tool as _cost_price_compose_envelope_tool,
    cost_rollup_hierarchy_tool as _cost_rollup_hierarchy_tool,
    cost_rollup_tool as _cost_rollup_tool,
    cost_select_quota_tool as _cost_select_quota_tool,
)
from norm import pipeline
from routing.orchestrator import orchestrate as _orchestrate

# 内网可信服务，关掉 DNS rebinding 保护——允许经 localhost / 服务器 IP 任意 Host 访问 :8101/mcp。
mcp = FastMCP(
    "ce-task",
    instructions=(
        "深圳房建任务层能力（按国标版本严格隔离 2013/2024）：\n"
        "  · orchestrate_tool —— 【前门】原始请求 → 确定性路由 → 单一直派 / 复合拆解-综合；不确定问谁、"
        "或一句话含多诉求时用它，自动分流（内部调下面两能力）\n"
        "  · norm_qa_tool —— 造价/计量/计价规范问题 → 检索条文 + 带引用结构化作答（零召回拒答，不编造条文）\n"
        "  · cost_compose_tool —— 构件描述 → 清单候选召回 → LLM 选码 → 组价取数（缺价标 no_source、"
        "选不出码转 HITL，绝不杜撰）\n"
        "  · quota_lookup_tool —— 【已知清单码】直查套定额取数（纯键查、不选码；从描述选码必走 cost_compose_tool）\n"
        "  · price_lookup_tool —— 【材料/人工/机械名】直查当期信息价（纯键查，零命中诚实返回）\n"
        "  · cost_compute_unit_price_tool / cost_rollup_tool / cost_rollup_hierarchy_tool —— 确定性算钱原语；"
        "费率/税率/工程量必须显式给，不杜撰\n"
        "  · cost_gate_decision_tool / cost_build_manual_quota_basis_tool / cost_select_quota_tool —— 组价 HITL 门控与"
        "套定额选择原语，返回结构化 need_human_input\n"
        "  · start_cost_session_tool —— 起可中断 HITL 完整组价会话（走到总造价、逐闸确认/录入）：只点火、"
        "返回结构化 task_id / interrupt / ui_hint 供前端内嵌控件驱动，你**不逐闸编排**"
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def norm_qa_tool(
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
def cost_compose_tool(
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
def quota_lookup_tool(
    code: Annotated[str, Field(description="已知清单编码（9 位），如「010502001」")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则默认深圳·2013（§4.0 不反问）")] = None,
    region: Annotated[str, Field(description="地区（当前仅深圳），用于定额过滤与信息价取价")] = "深圳",
) -> dict:
    """**已知清单编码** → 套定额取数（定额子目 + 工料机含量 + 信息价单价）：纯键查、**不选码**。

    **何时用**：用户**直接给了清单码**、只想查「这码套什么定额 / 工料机取数」——键查场景。
    **与 ``cost_compose_tool`` 分工（红线）**：从**构件描述**选码是判断题、有红线兜底，必须走 ``cost_compose_tool``；
    本工具**不做任何选码判断**，只对给定 code 确定性取数。若 code 有歧义/不确定，别用本工具猜，转 ``cost_compose_tool``。

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
def price_lookup_tool(
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
def cost_match_bill_item_tool(
    description: Annotated[str, Field(description="构件/做法描述，如「C30 现浇钢筋混凝土矩形柱」")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则默认深圳·2013")] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="清单候选召回数")] = 10,
) -> dict:
    """清单识别原语：描述 → 清单候选召回 → 候选内选码信封。

    这是 ``provenance.list_match`` 的 MCP 表面，适合编排层只想做“清单识别/选码”，暂时不取定额、不算钱。
    返回统一信封 ``{step,status,result,provenance}``；``status=need_review`` 时调用方应进入人工确认，
    不得把候选码当最终码继续取价。
    """
    try:
        return _cost_match_bill_item_tool(description, spec or COST_DEFAULT_SPEC, top_k)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"依赖服务不可达（ce-rag / LLM）: {exc}") from exc
    except ValueError as exc:
        raise ToolError(f"LLM 选码输出非合法 JSON: {exc}") from exc


@mcp.tool()
def cost_price_compose_envelope_tool(
    code: Annotated[str, Field(description="已确认清单编码；必须是用户确认或高置信选码结果")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则默认深圳·2013")] = None,
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
) -> dict:
    """组价取数信封原语：已确认清单码 → 定额子目信封 + 信息价材料块。

    这是 ``provenance.from_price_compose`` 的 MCP 表面。与 ``quota_lookup_tool`` 的区别：本工具返回图节点使用的
    HITL 友好结构，拆成 ``quota_envelope`` 和 ``materials``，方便编排层后续决定定额确认、缺价补录。
    """
    try:
        return _cost_price_compose_envelope_tool(region, code, spec or COST_DEFAULT_SPEC)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"ce-db 不可达（/price/compose）: {exc}") from exc


@mcp.tool()
def cost_compute_unit_price_tool(
    labor_cost: Annotated[float, Field(ge=0, description="人工费（元/计量单位）")],
    material_cost: Annotated[float, Field(ge=0, description="材料费（元/计量单位）")],
    machine_cost: Annotated[float, Field(ge=0, description="施工机具使用费（元/计量单位）")],
    management_fee_rate: Annotated[float, Field(ge=0, description="管理费率 %，调用方/HITL 显式给定")],
    profit_rate: Annotated[float, Field(ge=0, description="利润率 %，调用方/HITL 显式给定")],
    fee_base: Annotated[str, Field(description="取费基数：labor / labor_machine / lmm")],
    risk_rate: Annotated[float, Field(ge=0, description="风险费率 %")] = 0.0,
    quantity: Annotated[float, Field(gt=0, description="工程量 Q，必须显式给定；默认 1 仅表示单位价")] = 1.0,
    tax_rate: Annotated[float | None, Field(ge=0, description="增值税率 %；可选，通常总价汇总阶段统一计税")] = None,
) -> dict:
    """确定性综合单价计算 tool：人材机费 + 费率 + 工程量 → 综合单价/综合合价。

    不调用 LLM，不内置地区默认费率；非法金额、费率、取费基数由 ``UnitPriceInput`` 拦截。
    """
    try:
        return _cost_compute_unit_price_tool({
            "labor_cost": labor_cost,
            "material_cost": material_cost,
            "machine_cost": machine_cost,
            "management_fee_rate": management_fee_rate,
            "profit_rate": profit_rate,
            "risk_rate": risk_rate,
            "fee_base": fee_base,
            "quantity": quantity,
            "tax_rate": tax_rate,
        })
    except ValueError as exc:
        raise ToolError(f"综合单价入参非法: {exc}") from exc


@mcp.tool()
def cost_rollup_tool(
    subtotal: Annotated[float, Field(ge=0, description="分部分项合价（元）")],
    measure_fee: Annotated[float, Field(ge=0, description="措施项目费（元）")] = 0.0,
    other_fee: Annotated[float, Field(ge=0, description="其他项目费（元）")] = 0.0,
    fee_levy: Annotated[float, Field(ge=0, description="规费（元）")] = 0.0,
    tax_rate: Annotated[float | None, Field(ge=0, description="税金率 %；给定则算税金+总造价")] = None,
) -> dict:
    """确定性总造价汇总 tool：分部分项 + 措施 + 其他 + 规费 → 可选税金 → 总造价。"""
    try:
        return _cost_rollup_tool({
            "subtotal": subtotal,
            "measure_fee": measure_fee,
            "other_fee": other_fee,
            "fee_levy": fee_levy,
            "tax_rate": tax_rate,
        })
    except ValueError as exc:
        raise ToolError(f"汇总入参非法: {exc}") from exc


@mcp.tool()
def cost_rollup_hierarchy_tool(
    items: Annotated[
        list[dict],
        Field(description="构件成本行列表：{single_work, unit_work, total_price?, feature?}；total_price=None 表示未计价"),
    ],
    measure_fee: Annotated[float, Field(ge=0, description="措施项目费（元）")] = 0.0,
    other_fee: Annotated[float, Field(ge=0, description="其他项目费（元）")] = 0.0,
    fee_levy: Annotated[float, Field(ge=0, description="规费（元）")] = 0.0,
    tax_rate: Annotated[float | None, Field(ge=0, description="税金率 %；给定则算税金+总造价")] = None,
) -> dict:
    """确定性层级汇总 tool：构件行 → 单位工程 → 单项工程 → 项目总造价。

    ``total_price=None`` 的构件计入 missing，不计金额，绝不虚构未计价构件金额。
    """
    try:
        return _cost_rollup_hierarchy_tool({
            "items": items,
            "measure_fee": measure_fee,
            "other_fee": other_fee,
            "fee_levy": fee_levy,
            "tax_rate": tax_rate,
        })
    except ValueError as exc:
        raise ToolError(f"层级汇总入参非法: {exc}") from exc


@mcp.tool()
def cost_gate_decision_tool(
    gate: Annotated[
        str,
        Field(description="门控类型：coding / quota / price / quantity / rates / params / basis_complete / has_priceable_item"),
    ],
    payload: Annotated[dict, Field(description="对应门控输入数据，如 env/price/quantity/rates/params/basis/items")],
) -> dict:
    """组价 HITL 门控判断 tool：输入当前步骤数据 → 是否需要人工介入。

    返回统一 ``{needs_human_input, reason, gate, payload?}``。本工具只做门控判断/结构化 payload 生成，
    不推进会话、不落值、不调用 LLM。
    """
    try:
        return _cost_gate_decision_tool(gate, payload)
    except KeyError as exc:
        raise ToolError(f"门控 {gate!r} 缺少必填 payload 字段: {exc}") from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def cost_build_manual_quota_basis_tool(
    labor_cost: Annotated[float | None, Field(ge=0, description="定额人工费基价；与材料/机械三项要么全填，要么全空")] = None,
    material_cost: Annotated[float | None, Field(ge=0, description="定额材料费基价")] = None,
    machine_cost: Annotated[float | None, Field(ge=0, description="定额机械费基价")] = None,
    quota_code: Annotated[str | None, Field(description="定额子目号；未知可空，系统标为用户录入")] = None,
) -> dict:
    """人工补录定额基价规范化 tool。

    三项全齐 → 返回可喂 ``cost_compute_unit_price`` 的 ``basis``；三项全空 → 表示放弃补录；
    半填 → ``needs_human_input=true``，要求补齐或全空放弃。
    """
    return _cost_build_manual_quota_basis_tool(
        labor_cost=labor_cost,
        material_cost=material_cost,
        machine_cost=machine_cost,
        quota_code=quota_code,
    )


@mcp.tool()
def cost_select_quota_tool(
    quotas: Annotated[list[dict], Field(description="候选定额子目列表，每项至少含 子目号，可带人材机基价")],
    feature: Annotated[str | None, Field(description="构件/做法描述，用于未来套定额模型消歧")] = None,
    code: Annotated[str | None, Field(description="已确认清单编码")] = None,
    tau: Annotated[float, Field(ge=0, le=1, description="自动通过置信阈值")] = 0.75,
) -> dict:
    """套定额选择 tool：多定额候选内选择子目，未接入模型时明确转人工。

    当前默认未注入套定额模型，多子目会返回 ``need_review=true``；一旦服务端注册 selector，本 tool 自动复用。
    """
    return _cost_select_quota_tool(quotas, feature, code, tau)


@mcp.tool()
def start_cost_session_tool(
    feature: Annotated[str, Field(description="构件/做法描述，如「C30 现浇钢筋混凝土矩形柱」")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可缺省）：缺则默认口径深圳·2013"
                                                  "（§4.0 不反问）；2013 组价数据未就绪时会话仍起、走到取数处如实标未就绪")] = None,
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
) -> dict:
    """起一个**可中断 HITL 完整组价会话**（走到总造价），只点火、不逐闸编排（任务层 session.start 的 MCP 表面）。

    **何时用**：用户要「走完整组价流程、算到总造价、要人逐步确认编码/定额/录入费率」——与 ``cost_compose_tool``
    （一把出、只到选码+取数、不算钱）分工。**你（agent）只做一件事**：调本工具起会话，然后根据返回的
    ``task_id`` 告知用户会话已开始。前端识别结构化返回里的 ``interrupt`` 并内嵌渲染交互式组价控件，
    用户在控件里逐闸点选/录入，控件直接驱动会话（走 REST /cost/session/*），**全程不再经过你**。
    这是红线：组价 13 步编排在服务端的图里，弱模型不当编排器（不逐闸 resume、不替用户做闸内决策、不跳闸）。

    参数：feature —— 构件/做法描述；spec —— 国标版本 2013/2024（缺省默认深圳·2013）；region —— 地区（深圳）。
    返回：``{task_id, status, interrupt, ui_hint}``——interrupt 为首个闸 payload（供前端控件直接渲染）；
      ui_hint 给出会话入口/状态/恢复路径；status 为会话状态。
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
    return {
        "task_id": task_id,
        "status": result.get("status"),
        "interrupt": result.get("interrupt"),
        "ui_hint": {
            "kind": "inline_session",
            "start_path": "/ce-cost/session/start",
            "state_path": f"/ce-cost/session/{task_id}/state",
            "resume_path": f"/ce-cost/session/{task_id}/resume",
        },
    }


@mcp.tool()
def orchestrate_tool(
    query: Annotated[str, Field(description="用户请求（可复合，如「这做法套什么清单码，再解释它按什么计量、可否更省」）")],
    has_project_context: Annotated[
        bool | None,
        Field(description="是否已挂 BOQ/算量上下文（覆盖文本推断）；None 则由文本推断"),
    ] = None,
) -> dict:
    """四层骨架**前门**：确定性前置路由 → 单一直派能力层 / 复合 32b 拆解-综合。

    lead-agent 把**原始请求**整条交给本工具即可，无需自己判能力/拆子任务：
      - ① 前置路由（无 LLM）定 capability（组价/规范/价格/复合）+ 形态；
      - 单一意图 → 直派能力层 ②（norm_qa_tool 检索带引用 / cost 选码+组价取数），过 ③ 校验闸（meta.guard）；
      - 复合意图（EH-01）→ 32b 拆解 → 每子任务回 ① 路由 → 派发 → 32b 综合（降级安全：LLM 挂则确定性拼接）。

    **何时用哪个**：模糊/复合/不确定该问谁 → 本工具（前门，自动分流）；已确定要「检索规范条文」或
    「构件→清单码→价」的单一能力，可直接调 ``ce-task_norm_qa_tool`` / ``ce-task_cost_compose_tool`` 原语（非 chokepoint）。
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
