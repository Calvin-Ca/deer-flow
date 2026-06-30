"""任务层能力 MCP façade（FastMCP streamable-HTTP，任务服务 :8101 `/mcp`）。

在现有 HTTP REST（``/norm/qa`` / ``/cost/compose``）之外**加一层讲 MCP 协议的 façade**，让任意
deer-flow agent 把两个**无状态、一把出结果**的任务层能力当 tool 直接调，且工具名/入参/结果天然
结构化——前端 toolCall 流据此按工具名渲染领域结果（cited_clauses / 选码+置信度），不再 sniff
bash 命令、不再把结论埋在 bash stdout 里看不见。

与知识层 façade（``ce-code/service/mcp_server.py`` 的 ``ce-cost``，:8100/mcp，三个取数原语）分工：
  - ``ce-cost``（:8100）—— **纯数据原语**：bill_match / quota_lookup / price_compose；
  - ``ce-task``（本文件，:8101）—— **带 LLM 编排的任务层能力**：norm_qa（检索+带引用作答）、
    cost_compose（候选召回 → LLM 选码 → 组价取数）。

设计要点（与知识层 façade 一致）：
  - **复用同一份编排内核**：两个 ``@mcp.tool`` 直接调 ``knowledge_client.search`` + ``generation.answer``
    与 ``orchestration.compose``，**不反代自家 REST**（不再起一跳 HTTP 打 :8101 自己）。
  - **红线下沉到工具边界**：``spec``/``standard`` 必填无默认（逼调用方选国标版本，杜绝 2013/2024
    串库）；零召回不喂空上下文给 LLM 编答案（norm_qa 直返「无依据」）；选不出码 need_review、缺价
    no_source、2013 未就绪等如实透传（compose 内核已保证），不杜撰。
  - **无状态**（``stateless_http=True``）：每请求一新 transport。HITL 可中断组价会话**不在此暴露**
    （那是有状态 + 交互式，已由前端内嵌 ``cost-hitl`` marker 卡片驱动，见 cost/router.py session 端点）。

工具名前缀：deer-flow 加载 MCP 工具时按 server 名加前缀（``ce-task`` → agent 见 ``ce-task_norm_qa``
``ce-task_cost_compose``）。

注册（``extensions_config.json`` 的 ``mcpServers``）::

    "ce-task": {
      "enabled": true,
      "type": "http",
      "url": "http://localhost:8101/mcp",
      "description": "深圳房建任务层能力：norm_qa（规范问答）/ cost_compose（选码+组价取数）"
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

from common import knowledge_client
from common.config import LLM_MODEL_ID, LLM_URL
from cost import orchestration
from norm import generation

# 内网可信服务，关掉 DNS rebinding 保护——允许经 localhost / 服务器 IP 任意 Host 访问 :8101/mcp。
mcp = FastMCP(
    "ce-task",
    instructions=(
        "深圳房建任务层能力（按国标版本严格隔离 2013/2024）：\n"
        "  · norm_qa —— 造价/计量/计价规范问题 → 检索条文 + 带引用结构化作答（零召回拒答，不编造条文）\n"
        "  · cost_compose —— 构件描述 → 清单候选召回 → LLM 选码 → 组价取数（缺价标 no_source、"
        "选不出码转 HITL，绝不杜撰）"
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def norm_qa(
    query: Annotated[str, Field(description="造价/计量/计价类自然语言问题，如「现浇混凝土柱按什么计量」")],
    standard: Annotated[
        str,
        Field(description="规范代号（必填，无默认）：如 gb50854-2024 / gb50500-2013。按国标版本隔离，避免错版串库"),
    ],
    top_k: Annotated[int, Field(ge=1, le=50, description="检索召回条数")] = 15,
    skip_rerank: Annotated[bool, Field(description="跳过 cross-encoder 精排（调试用）")] = False,
) -> dict:
    """造价规范条文检索 + Qwen3 带引用作答（任务层 /norm/qa 的 MCP 表面）。

    **红线**：只引检索到的条文、零召回不喂空上下文给 LLM（直返「未检索到」），不编造条文号/原文。

    参数：
        query —— 造价/计量/计价自然语言问题。
        standard —— 规范代号（必填，无默认）：gb50854-2024 / gb50500-2013 等，按版本隔离。
        top_k —— 检索召回条数（1–50）。
        skip_rerank —— 跳过精排（调试）。
    返回：``{answer, cited_clauses:[{clause,standard,text,relevance}...], uncertain_aspects,
        out_of_scope_warnings, meta:{standard,retrieved,...}}``；零召回时 answer 为「未检索到」、
        cited_clauses 空（这是正确行为，非错误）。
    异常：未知规范 / 索引未就绪 / 知识服务不可达 / LLM 不可达或输出非法 JSON → ToolError。
    """
    try:
        search_resp = knowledge_client.search(
            query, standard=standard, top_k=top_k, skip_rerank=skip_rerank,
        )
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"知识服务检索失败: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"知识服务不可达: {exc}") from exc

    clauses = search_resp.get("clauses", [])
    if not clauses:
        # 零召回：不喂空上下文给 LLM 编答案，直接返回「无依据」（与 /norm/qa 一致）。
        return {
            "answer": "未在所选造价规范中检索到与问题相关的条文，无法作答。本回答仅供参考，不替代专业造价审核。",
            "cited_clauses": [],
            "uncertain_aspects": ["检索零召回，可能问题超出该规范范围或需换用其它规范"],
            "out_of_scope_warnings": [],
            "meta": {"standard": standard, "retrieved": 0, "search_meta": search_resp.get("meta", {})},
        }

    try:
        result = generation.answer(query, clauses, LLM_URL, LLM_MODEL_ID)
    except requests.RequestException as exc:
        raise ToolError(f"LLM 生成服务不可达: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 子类
        raise ToolError(f"LLM 输出非合法 JSON: {exc}") from exc

    result["meta"] = {
        "standard": standard,
        "retrieved": len(clauses),
        "search_meta": search_resp.get("meta", {}),
    }
    return result


@mcp.tool()
def cost_compose(
    description: Annotated[str, Field(description="构件/做法的自然语言描述，如「C30 现浇钢筋混凝土矩形柱」")],
    spec: Annotated[str, Field(description="国标版本（必填，无默认）：2013 / 2024。按版本隔离清单库/组价取数，避免串库")],
    region: Annotated[str, Field(description="地区（当前仅深圳），用于定额过滤与信息价取价")] = "深圳",
    top_k: Annotated[int, Field(ge=1, le=50, description="清单候选召回数")] = 10,
) -> dict:
    """构件描述 → 候选召回 → LLM 选码 → 组价取数（任务层 /cost/compose 的 MCP 表面，P1 选码闭环）。

    **红线**：选不出码（need_review / code=None）→ 不调组价、price_status="skipped(need_review)"，转 HITL；
    缺信息价的工料机 ``unit_price=None`` + ``price_status="no_source"``，绝不杜撰；2013 组价数据未就绪 →
    仅返回选码、price_status 标未就绪。**本工具不算综合单价/总造价**（费率/税率是政策数，走 HITL 录入）。

    参数：
        description —— 构件/做法描述；spec —— 国标版本（必填）：2013 / 2024；
        region —— 地区（深圳）；top_k —— 候选召回数（1–50）。
    返回：``{description, spec, region, candidates_count, selection:{code,confidence,reason,
        need_review,alternatives}, code, price, price_status}``；price 为组价取数结果
        （定额 + 工料机含量 + 信息价单价），未就绪/选不出码时为 None。
    异常：未知 spec / 知识服务不可达 / 清单项不存在 / LLM 不可达或输出非法 JSON → ToolError。
    """
    try:
        return orchestration.compose(
            description, spec, region, LLM_URL, LLM_MODEL_ID, top_k=top_k,
        )
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ToolError(f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"依赖服务不可达（知识服务 :8100 / LLM :8099）: {exc}") from exc
    except ValueError as exc:  # call_qwen3 的 json.JSONDecodeError
        raise ToolError(f"LLM 选码输出非合法 JSON: {exc}") from exc


if __name__ == "__main__":
    # 独立单跑（调试）：仅 MCP 端点，streamable-HTTP，默认挂 settings.streamable_http_path (/mcp)。
    # 正式随 main.py 一起 :8101（见该文件 app.mount("/", ...) + lifespan）。
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8101
    mcp.run(transport="streamable-http")
