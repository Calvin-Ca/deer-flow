"""ce-rag MCP façade: mixed retrieval and evidence lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from rag.service import RagService

_ROOT = Path(__file__).resolve().parent.parent
_VECTOR_STORE = _ROOT / "data" / "vector_store"
_svc = RagService(_VECTOR_STORE)

mcp = FastMCP(
    "ce-rag",
    instructions=(
        "面向证据检索与候选召回的知识服务：\n"
        "  · search_clause —— 规范条文混合检索\n"
        "  · expand_clause_refs —— 条文引用扩展\n"
        "  · get_clause —— 条款号直取\n"
        "  · match_bill_item —— 构件描述 → 清单候选召回\n"
        "  · search_aux_table —— 辅助/参数表投影检索\n"
        "  · search_price_rule —— 费用构成/费率规则投影检索\n"
        "  · retrieve_evidence —— 统一证据前门\n"
        "所有结果带 evidence / truth_level；本服务给候选与依据，不直接宣称最终真值。 "
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def search_clause(
    query: Annotated[str, Field(description="规范问句，如“现浇混凝土柱按什么计量”")],
    standard: Annotated[str, Field(description="规范代号，如 gb50854-2024")],
    top_k: Annotated[int, Field(ge=1, le=50, description="返回条数")] = 15,
    skip_rerank: Annotated[bool, Field(description="跳过 rerank（调试）")] = False,
) -> dict[str, Any]:
    try:
        return _svc.search_clause(query, standard=standard, top_k=top_k, skip_rerank=skip_rerank)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def expand_clause_refs(
    standard: Annotated[str, Field(description="规范代号，如 gb50854-2024")],
    node_paths: Annotated[list[str], Field(description="种子条款号列表")],
) -> dict[str, Any]:
    try:
        return _svc.expand_clause_refs(standard=standard, node_paths=node_paths)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def get_clause(
    standard: Annotated[str, Field(description="规范代号，如 gb50854-2024")],
    node_path: Annotated[str, Field(description="条款号，如 4.1.2")],
) -> dict[str, Any]:
    try:
        return _svc.get_clause(standard=standard, node_path=node_path)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def match_bill_item(
    description: Annotated[str, Field(description="构件/做法描述，如“C30现浇钢筋混凝土矩形柱”")],
    spec: Annotated[str, Field(description="国标版本 2013 / 2024（必填）")],
    top_k: Annotated[int, Field(ge=1, le=50, description="返回候选数")] = 10,
    code_prefixes: Annotated[list[str] | None, Field(description="专业 code 前缀过滤")] = None,
) -> dict[str, Any]:
    try:
        return _svc.match_bill_item(description, spec=spec, top_k=top_k, code_prefixes=code_prefixes)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def search_aux_table(
    query: Annotated[str, Field(description="查表意图，如“土分类表”")],
    spec: Annotated[str | None, Field(description="国标版本 2013 / 2024（可选）")] = None,
    chapter: Annotated[str | None, Field(description="章节过滤（可选）")] = None,
    kind: Annotated[str | None, Field(description="kind 过滤：classification / parameter / unknown")] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="返回条数")] = 10,
) -> dict[str, Any]:
    try:
        return _svc.search_aux_table(query, spec=spec, chapter=chapter, kind=kind, top_k=top_k)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def search_price_rule(
    query: Annotated[str, Field(description="规则查询，如“综合单价构成”“增值税费率”")],
    spec_version: Annotated[str | None, Field(description="结构化规则版本过滤（可选）")] = None,
    region: Annotated[str | None, Field(description="地区过滤（费率规则可选）")] = None,
    top_k: Annotated[int, Field(ge=1, le=50, description="返回条数")] = 10,
) -> dict[str, Any]:
    try:
        return _svc.search_price_rule(query, spec_version=spec_version, region=region, top_k=top_k)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def retrieve_evidence(
    query: Annotated[str, Field(description="统一证据查询文本")],
    corpus: Annotated[str, Field(description="证据域：clause / bill_match / aux_table / price_rule")],
    top_k: Annotated[int, Field(ge=1, le=50, description="返回条数")] = 10,
    standard: Annotated[str | None, Field(description="corpus=clause 时传规范代号")] = None,
    spec: Annotated[str | None, Field(description="corpus=bill_match 或 aux_table 时传国标版本")] = None,
    filters: Annotated[dict[str, Any] | None, Field(description="附加过滤，如 chapter/code_prefixes/region")] = None,
) -> dict[str, Any]:
    try:
        return _svc.retrieve_evidence(query, corpus=corpus, top_k=top_k, standard=standard, spec=spec, filters=filters)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8100
    mcp.run(transport="streamable-http")
