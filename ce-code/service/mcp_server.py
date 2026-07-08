"""组价取数原语 MCP façade（FastMCP streamable-HTTP，知识层 :8100 `/mcp`）。

兼容层说明：
  - 本文件保留历史 `ce-cost` 复合 MCP，供现有上层依赖继续工作。
  - 新拆分方案下，语义候选召回改走 `service.rag_mcp_server`（`ce-rag`），结构化真值取数改走
    `service.db_mcp_server`（`ce-db`）；设计文档见 `ce-code/MCP_SPLIT_PLAN.md`。

把三个取数原语在现有 HTTP REST（``service.cost_api``）之外**加一层讲 MCP 协议的 façade**，
让任意 deer-flow agent 把它们当 tool 直接调（应对「只查价」「只选码候选」等中间步请求，
以及将来强模型自由编排）。方案见 ``DEV.md §7「原语对外暴露：HTTP + MCP 双 façade」``。

设计要点（与方案一一对应）：
  - **复用同一份取数内核**：三个 ``@mcp.tool`` 只调 ``cost.bill_match.search_bill`` /
    ``cost.query.get_quota`` / ``cost.query.compose_price``，不重写 SQL/向量逻辑，**不反代 REST**。
  - **红线下沉到原语边界**（不依赖上层编排，agent 直接打单个原语也拦得住）：
      · ``spec`` 必填、无默认（``config.resolve_spec``）——逼调用方选国标版本，杜绝 2013/2024 串库；
      · ``price_compose`` 未命中信息价 → ``unit_price=None`` + ``price_status="no_source"``，绝不杜撰（取数内核已保证）；
      · 2013 ``supports_compose=False`` → ``price_compose``/``quota_lookup`` 结构化拒答（组价/定额数据未就绪）；
      · ``bill_match`` 只召回候选、不定 Top-1（选码归任务层）。
  - **无状态只读**（``stateless_http=True``）：每请求一新 transport，无需 session_pool。

工具名前缀：deer-flow 加载 MCP 工具时按 server 名加前缀（``ce-cost`` → agent 见 ``ce-cost_bill_match`` 等）。

注册（``extensions_config.json`` 的 ``mcpServers``）::

    "ce-cost": {
      "enabled": true,
      "type": "http",
      "url": "http://localhost:8100/mcp",
      "description": "深圳房建组价取数原语：bill_match / quota_lookup / price_compose"
    }

挂载/启动：
  - 正式：随 ``service.knowledge_api`` 一起对外 :8100，挂在 ``/mcp``（见该文件 ``app.mount``）。
  - 独立单跑（调试）：从 ce-code 根 ``.venv/bin/python -m service.mcp_server``（默认 :8100，仅 MCP）。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

import config
from cost import bill_match as bill_match_mod
from cost import query as cost_query

# 内网可信服务，关掉 DNS rebinding 保护——允许经 localhost / 服务器 IP 任意 Host 访问 :8100/mcp。
mcp = FastMCP(
    "ce-cost",
    instructions=(
        "深圳房建组价取数原语（按国标版本 spec 严格隔离 2013/2024）：\n"
        "  · bill_match —— 构件描述 → 清单候选召回（只给候选，选码归调用方）\n"
        "  · quota_lookup —— 定额子目 + 人材机含量直取\n"
        "  · price_compose —— 清单 → 定额 → 含量 ⋈ 信息价（缺价标 no_source，绝不杜撰）\n"
        "  · price_query —— 当期信息价查询（名称模糊 + 期号；动态数据无关 spec，零命中如实返回空）"
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _require_compose_ready(spec: str) -> dict:
    """校验 spec 合法且该版本组价/定额数据已就绪，返回路由配置；否则抛 ToolError（结构化拒答）。

    参数：spec —— 国标版本号字符串（"2013"/"2024"，必填无默认）。
    返回：``config.SPEC_REGISTRY`` 中该版本的路由配置 dict。
    异常：spec 缺省/未知 → ToolError（红线：版本不猜）；该版本 supports_compose=False → ToolError
      （组价/定额数据未就绪，仅 bill_match 可用）。
    """
    try:
        cfg = config.resolve_spec(spec)
    except ValueError as exc:                          # 缺省 / 未知 spec
        raise ToolError(str(exc)) from exc
    if not cfg["supports_compose"]:                    # 该版本定额/价格/映射未就绪
        raise ToolError(
            f"{cfg['label']} 组价/定额数据未就绪，暂仅支持清单匹配 bill_match（spec={spec}）"
        )
    return cfg


@mcp.tool()
def bill_match(
    description: Annotated[str, Field(description="构件/做法的自然语言描述，如「C30 现浇钢筋混凝土矩形柱」")],
    spec: Annotated[str, Field(description="国标版本（必填，无默认）：2013 / 2024。按版本路由到对应清单库，避免跨版本串库")],
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
    top_k: Annotated[int, Field(ge=1, le=50, description="返回候选数")] = 10,
    code_prefixes: Annotated[
        list[str] | None,
        Field(description="专业 code 前缀过滤，如 ['01','03'] 只在建筑+安装内召回；不传=全专业"),
    ] = None,
) -> dict:
    """构件描述 → 清单候选召回（dense 向量检索 bill_spec_kb，结构约束重排）。

    **红线**：知识层只召回候选、不定 Top-1。选码（含「不造码 / 低置信转人工 / 空候选转人工」）
    归任务层决策，本原语不越界做选码。

    参数：
        description —— 构件/做法自然语言描述。
        spec —— 国标版本（必填，无默认）：2013 / 2024，按版本路由 collection，避免串库。
        region —— 地区（当前仅深圳，预留）。
        top_k —— 返回候选数（1–50）。
        code_prefixes —— 专业 code 前缀过滤（如 ['01','03']）；None=全专业。
    返回：``{"query","spec","region","count","candidates":[{code,name,unit,feature,chapter,
        doc_id,spec_version,cast_type,score}...]}``，按相似度降序。
    异常：spec 缺省/未知 → ToolError；向量库未就绪 / Milvus / 嵌入服务不可达 → ToolError。
    """
    try:
        cfg = config.resolve_spec(spec)                # 国标版本路由（缺省/未知 → ToolError）
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    try:
        candidates = bill_match_mod.search_bill(
            description, top_k,
            collection_name=cfg["bill_collection"],
            structural=True,
            code_prefixes=code_prefixes,
        )
    except ValueError as exc:                          # collection 未就绪（语义化「索引未就绪」）
        raise ToolError(f"清单候选库未就绪: {exc}") from exc
    except Exception as exc:                           # Milvus / 嵌入服务不可达
        raise ToolError(f"清单召回服务不可达: {exc}") from exc
    return {
        "query": description, "spec": spec, "region": region,
        "count": len(candidates), "candidates": candidates,
    }


@mcp.tool()
def price_query(
    name: Annotated[str, Field(description="材料/人工/机械名称（子串模糊匹配，如「钢筋」「商品混凝土」）")],
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
    period: Annotated[str | None, Field(description="期号 YYYY-MM（如 2026-05）；缺省各资源取最新期")] = None,
    category: Annotated[str | None, Field(description="类别过滤：人工 / 材料 / 机械（可选）")] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="返回行数上限")] = 10,
) -> dict:
    """当期信息价查询（FR-I01）：名称模糊匹配 → 登载价 + 期段时效。纯数据访问原语，确定性。

    **红线**：零命中如实返回 ``count=0``（no_source，调用方不得据此编价）；价格为月刊登载值
    原样返回，不做差价/合价计算（C-04 归计算工具）。信息价是动态数据、与国标版本无关（无 spec 参数），
    地域口径由 region 锁定（C-02）。

    参数：name —— 名称（必填）；region —— 地区；period —— 期号 YYYY-MM（缺省最新期）；
        category —— 人工/材料/机械 过滤；top_k —— 行数上限（1–50）。
    返回：``{"name","region","period","count","results":[{name,spec,unit,category,price,price_type,
        period_start,period_end,doc_id}...]}``。
    异常：name 为空 / period 格式非法 → ToolError；PG 不可达 → ToolError。
    """
    if not name.strip():
        raise ToolError("name 不能为空")
    if period is not None and not re.fullmatch(r"20\d{2}-\d{2}", period):
        raise ToolError("period 须为 YYYY-MM（如 2026-05）")
    try:
        with cost_query.connect() as conn:
            rows = cost_query.query_resource_price(conn, name.strip(), region=region,
                                                   period=period, category=category, top_k=top_k)
    except Exception as exc:                           # psycopg.OperationalError 等：PG 不可达
        raise ToolError(f"造价库不可达: {exc}") from exc
    return {"name": name, "region": region, "period": period, "count": len(rows), "results": rows}


@mcp.tool()
def quota_lookup(
    code: Annotated[str, Field(description="定额子目编号，如 010001-3")],
    spec: Annotated[str, Field(description="国标版本（必填，无默认）：2013 / 2024。2013 定额数据未就绪将拒答")],
    region: Annotated[str, Field(description="地区（当前仅深圳）")] = "深圳",
) -> dict:
    """按地区 + 定额子目编号直取定额（子目字段 + 工料机含量）。纯数据访问原语，确定性。

    参数：
        code —— 定额子目编号（如 010001-3）。
        spec —— 国标版本（必填，无默认）：2013 / 2024。**红线**：版本不猜；2013 定额数据未就绪 → 拒答。
        region —— 地区（如 深圳）。
    返回：``{"item":{...子目字段...}, "resources":[...人材机含量...]}``。
    异常：spec 缺省/未知或该版本定额未就绪 → ToolError；子目不存在 → ToolError(404 语义)；
        PG 不可达 → ToolError。
    """
    _require_compose_ready(spec)                       # spec 必填 + 2013 定额未就绪拒答（红线）
    try:
        with cost_query.connect() as conn:
            result = cost_query.get_quota(conn, region, code)
    except Exception as exc:                           # psycopg.OperationalError 等：PG 不可达
        raise ToolError(f"造价库不可达: {exc}") from exc
    if result is None:
        raise ToolError(f"定额子目 {code}（{region}）不存在")
    return result


@mcp.tool()
def price_compose(
    code: Annotated[str, Field(description="清单编码（GB 50854，9 位）")],
    spec: Annotated[str, Field(description="国标版本（必填，无默认）：2013 / 2024。2013 组价数据未就绪将拒答")],
    region: Annotated[str, Field(description="地区（当前仅深圳），同时用于定额过滤与信息价取价")] = "深圳",
    on_date: Annotated[
        date | None,
        Field(description="计价期（ISO 日期，可选）；缺省每资源取最新可用信息价期"),
    ] = None,
) -> dict:
    """组价取数：清单项 → 适用定额 → 工料机含量 + 信息价单价（含小计）。

    **红线**：未命中信息价的工料机 ``unit_price=None`` + ``price_status="no_source"``（绝不杜撰价，
    交任务层 HITL 询价）；2013 组价数据未就绪 → 拒答（仅 bill_match 可用）。

    参数：
        code —— 清单编码（9 位）。
        spec —— 国标版本（必填，无默认）：2013 / 2024。版本不猜；按版本隔离 bill_spec 取数。
        region —— 地区（如 深圳）。
        on_date —— 计价期（可选）；None=每资源取最新可用信息价期。
    返回：见 ``cost.query.compose_price``——``{"bill":{...}, "region","on_date","quota_count",
        "quotas":[{quota_code,name,unit,confidence,source,人材机费,"resources":[{...,unit_price,
        price_status,amount}...]}...]}``；price_status: matched / no_source。
    异常：spec 缺省/未知或 2013 组价未就绪 → ToolError；清单项不存在 → ToolError(404 语义)；
        PG 不可达 → ToolError。清单存在但无映射定额 → quotas 为空列表（供调用方感知覆盖缺口）。
    """
    cfg = _require_compose_ready(spec)                 # spec 必填 + 2013 组价未就绪拒答（红线）
    try:
        with cost_query.connect() as conn:
            result = cost_query.compose_price(
                conn, region, code, on_date,
                spec_versions=cfg["bill_spec_versions"],
            )
    except Exception as exc:                           # psycopg.OperationalError 等：PG 不可达
        raise ToolError(f"造价库不可达: {exc}") from exc
    if result is None:
        raise ToolError(f"清单项 {code} 不存在（{cfg['label']}）")
    return result


if __name__ == "__main__":
    # 独立单跑（调试）：仅 MCP 端点，streamable-HTTP，默认挂 settings.streamable_http_path (/mcp)。
    # 正式随 service.knowledge_api 一起 :8100（见该文件 app.mount("/mcp", ...)）。
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8100
    mcp.run(transport="streamable-http")
