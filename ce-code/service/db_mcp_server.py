"""ce-db MCP façade: readonly structured data access."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from db.service import DbService

_svc = DbService()

mcp = FastMCP(
    "ce-db",
    instructions=(
        "面向结构化真值取数的知识服务：\n"
        "  · bill_get / bill_list —— 清单规范\n"
        "  · quota_get / quota_list —— 定额子目\n"
        "  · price_query / price_compose —— 信息价与组价取数\n"
        "  · fee_rate_lookup / price_composition_get —— 费率与费用构成\n"
        "  · aux_table_get / aux_table_list —— 辅助表\n"
        "  · resource_lookup —— 人材机主数据\n"
        "这是只读确定性服务；不做语义猜测，不提供 raw_sql。"
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def bill_get(
    code: Annotated[str, Field(description="9 位清单编码")],
    spec: Annotated[str, Field(description="国标版本 2013 / 2024（必填）")],
) -> dict[str, Any]:
    try:
        result = _svc.bill_get(code, spec=spec)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    if result is None:
        raise ToolError(f"清单编码 {code} 不存在")
    return result


@mcp.tool()
def bill_list(
    spec: Annotated[str, Field(description="国标版本 2013 / 2024（必填）")],
    code_prefix: Annotated[str | None, Field(description="编码前缀过滤")] = None,
    chapter: Annotated[str | None, Field(description="章节过滤")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="返回条数")] = 20,
) -> list[dict[str, Any]]:
    try:
        return _svc.bill_list(spec=spec, code_prefix=code_prefix, chapter=chapter, limit=limit)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def quota_get(
    region: Annotated[str, Field(description="地区，如 深圳")],
    code: Annotated[str, Field(description="定额子目编号")],
) -> dict[str, Any]:
    try:
        result = _svc.quota_get(region=region, code=code)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    if result is None:
        raise ToolError(f"定额子目 {code}（{region}）不存在")
    return result


@mcp.tool()
def quota_list(
    region: Annotated[str, Field(description="地区（默认深圳）")] = "深圳",
    spec: Annotated[str | None, Field(description="国标版本（可选，仅按 bill_code 关联时建议传）")] = None,
    bill_code: Annotated[str | None, Field(description="关联清单编码（可选）")] = None,
    chapter: Annotated[str | None, Field(description="章节过滤")] = None,
    quota_code_prefix: Annotated[str | None, Field(description="定额编码前缀过滤")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="返回条数")] = 20,
) -> list[dict[str, Any]]:
    try:
        return _svc.quota_list(
            region=region,
            spec=spec,
            bill_code=bill_code,
            chapter=chapter,
            quota_code_prefix=quota_code_prefix,
            limit=limit,
        )
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def price_query(
    name: Annotated[str, Field(description="材料/人工/机械名称")],
    region: Annotated[str, Field(description="地区（默认深圳）")] = "深圳",
    period: Annotated[str | None, Field(description="期号 YYYY-MM（可选）")] = None,
    category: Annotated[str | None, Field(description="类别：人工 / 材料 / 机械（可选）")] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="返回条数")] = 10,
) -> dict[str, Any]:
    try:
        return _svc.price_query(name=name, region=region, period=period, category=category, top_k=top_k)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def price_compose(
    code: Annotated[str, Field(description="9 位清单编码")],
    spec: Annotated[str, Field(description="国标版本 2013 / 2024（必填）")],
    region: Annotated[str, Field(description="地区（默认深圳）")] = "深圳",
    on_date: Annotated[date | None, Field(description="计价日期，可选")] = None,
) -> dict[str, Any]:
    try:
        result = _svc.price_compose(code=code, spec=spec, region=region, on_date=on_date)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    if result is None:
        raise ToolError(f"清单编码 {code} 不存在")
    return result


@mcp.tool()
def fee_rate_lookup(
    region: Annotated[str, Field(description="地区（默认深圳）")] = "深圳",
    fee_category: Annotated[str | None, Field(description="费用大类过滤")] = None,
    fee_name: Annotated[str | None, Field(description="费用名过滤")] = None,
    applicable: Annotated[str | None, Field(description="适用范围过滤")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="返回条数")] = 20,
) -> list[dict[str, Any]]:
    try:
        return _svc.fee_rate_lookup(
            region=region,
            fee_category=fee_category,
            fee_name=fee_name,
            applicable=applicable,
            limit=limit,
        )
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def price_composition_get(
    composite: Annotated[str | None, Field(description="如 综合单价 / 工程造价")] = None,
    spec_version: Annotated[str | None, Field(description="规则版本过滤")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="返回条数")] = 50,
) -> list[dict[str, Any]]:
    try:
        return _svc.price_composition_get(composite=composite, spec_version=spec_version, limit=limit)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def aux_table_get(
    doc_id: Annotated[str, Field(description="文档 ID，如 GB-50854")],
    caption: Annotated[str, Field(description="表标题，需精确匹配")],
    chapter: Annotated[str | None, Field(description="章节（可选）")] = None,
) -> dict[str, Any]:
    try:
        result = _svc.aux_table_get(doc_id=doc_id, caption=caption, chapter=chapter)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    if result is None:
        raise ToolError(f"辅助表不存在：doc_id={doc_id}, caption={caption}")
    return result


@mcp.tool()
def aux_table_list(
    spec_version: Annotated[str | None, Field(description="规范版本过滤")] = None,
    doc_id: Annotated[str | None, Field(description="文档 ID 过滤")] = None,
    kind: Annotated[str | None, Field(description="kind 过滤")] = None,
    chapter: Annotated[str | None, Field(description="章节过滤")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="返回条数")] = 20,
) -> list[dict[str, Any]]:
    try:
        return _svc.aux_table_list(
            spec_version=spec_version,
            doc_id=doc_id,
            kind=kind,
            chapter=chapter,
            limit=limit,
        )
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def resource_lookup(
    name: Annotated[str, Field(description="资源名称模糊匹配")],
    category: Annotated[str | None, Field(description="类别过滤")] = None,
    unit: Annotated[str | None, Field(description="单位过滤")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="返回条数")] = 20,
) -> list[dict[str, Any]]:
    try:
        return _svc.resource_lookup(name=name, category=category, unit=unit, limit=limit)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8102
    mcp.run(transport="streamable-http")
