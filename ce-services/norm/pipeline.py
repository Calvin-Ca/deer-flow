"""Norm-QA 编排内核 —— 规范问答的单一实现（被 /norm/qa 端点、ce-task_norm_qa MCP 工具、
复合编排器 T-A4 共用）。

链路：``resolve_standard``（T-A2 规范选择确定化）→ ``knowledge_client.search``（:8100 检索）→
零召回则 **先 ``web_fallback``（FR-K07 联网兜底三道闸，仅本 norm 链路）**、仍无可信命中才
``guards.reject_no_recall``（C-03 拒答给出路）/ 否则 ``generation.answer``（Qwen3 带引用）→
``guards.audit_answer``（C-01/C-02 校验闸）→ 组装 meta。

抽出动机：此前 ``norm/router.py`` 与 ``common/mcp_server.py`` 各持一份**近乎一致**的实现，T-A4 还需
第三个调用点（编排器派发 norm 子任务）——三处复制必漂移，故收敛到本模块单一事实源。调用方只需
包一层「异常 → 自己的错误类型」映射（端点→HTTPException、MCP→ToolError）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from common import knowledge_client
from common.config import NORM_WEB_FALLBACK
from norm import generation, guards, web_fallback
from norm.standard_router import resolve_standard

logger = logging.getLogger("ce-services.norm")


def answer_query(
    query: str,
    *,
    standard_hint: str | None,
    llm_url: str,
    model_id: str,
    top_k: int = 15,
    skip_rerank: bool = False,
) -> dict[str, Any]:
    """规范问答完整链路 → 结构化回答 dict（含 meta）。

    参数：
        query —— 造价/计量/计价自然语言问题。
        standard_hint —— 规范代号 hint（可选）；family 由 ``resolve_standard`` 确定性裁定（T-A2）。
        llm_url / model_id —— Qwen3 vLLM 配置。
        top_k / skip_rerank —— 检索参数。
    返回：``{answer, cited_clauses, uncertain_aspects, out_of_scope_warnings, meta}``；meta 含
        ``standard / retrieved / retrieve_ms / generate_ms / standard_resolution / guard / search_meta``。
    异常：检索 ``requests.HTTPError``/``RequestException``、生成 ``RequestException``/``ValueError``
        原样上抛，由调用方映射为各自错误类型。
    """
    t0 = time.perf_counter()
    # ⓪ 规范选择确定化（T-A2）：hint 仅备选，family 与 hint 冲突时确定性夺回（治标准漂移）。
    resolution = resolve_standard(query, hint=standard_hint)
    standard = resolution.standard
    if resolution.overrode_hint:
        logger.warning("standard 漂移拦截：hint=%s → 确定性夺回 %s（intent=%s, matched=%s）",
                       standard_hint, standard, resolution.intent, resolution.matched)

    # ① 检索（异常上抛由调用方映射）
    search_resp = knowledge_client.search(
        query, standard=standard, top_k=top_k, skip_rerank=skip_rerank,
    )
    clauses = search_resp.get("clauses", [])
    search_meta = search_resp.get("meta", {})
    retrieve_ms = (time.perf_counter() - t0) * 1000

    # ③ 零召回：先联网兜底（FR-K07 三道闸，仅 norm 链路；组价/价格永不联网），仍无可信命中
    # 才落 C-03 拒答（给出路：已查范围 = 本地库 + 联网）。
    if not clauses:
        if NORM_WEB_FALLBACK:
            try:
                web_result, web_report = web_fallback.answer_from_web(
                    query, standard, llm_url=llm_url, model_id=model_id)
            except Exception as exc:  # 兜底路径绝不反噬主链路：任何意外 → 当作无可信命中
                logger.warning("联网兜底异常（降级拒答）：%s", exc)
                web_result, web_report = None, None
            if web_result is not None:
                web_ms = (time.perf_counter() - t0) * 1000 - retrieve_ms
                web_result["meta"] = {
                    "standard": standard, "retrieved": 0, "retrieve_ms": round(retrieve_ms),
                    "web_ms": round(web_ms), "standard_resolution": resolution.as_meta(),
                    "guard": web_report.as_meta(), "search_meta": search_meta,
                }
                logger.info("norm pipeline standard=%s retrieved=0 → web fallback tier=web citations=%d",
                            standard, len(web_result.get("web_citations", [])))
                return web_result
            searched_scope = ["本地规范库", "联网检索（无可信命中或不可用）"]
        else:
            searched_scope = ["本地规范库（联网兜底已关闭）"]
        result, report = guards.reject_no_recall(
            standard, search_meta=search_meta,
            extra_aspects=[f"已查范围：{' + '.join(searched_scope)}"])
        result["meta"] = {
            "standard": standard, "retrieved": 0, "retrieve_ms": round(retrieve_ms),
            "standard_resolution": resolution.as_meta(), "guard": report.as_meta(),
            "search_meta": search_meta,
        }
        logger.info("norm pipeline standard=%s retrieved=0 verdict=%s", standard, report.verdict)
        return result

    # ② 生成：检索条文 → 结构化带引用回答
    t1 = time.perf_counter()
    result = generation.answer(query, clauses, llm_url, model_id)
    generate_ms = (time.perf_counter() - t1) * 1000

    # ③ C-01/C-02 校验闸（生成后）：剔除他部/跨版条文、规范化溯源；全污染则降级拒答。
    result, report = guards.audit_answer(result, resolved_standard=standard, search_meta=search_meta)
    if report.violations:
        logger.warning("norm pipeline 校验闸 standard=%s verdict=%s 剔除=%d 违规=%s",
                       standard, report.verdict, report.cited_dropped, report.violations)
    logger.info("norm pipeline standard=%s (hint=%s src=%s) retrieved=%d verdict=%s retrieve=%.0fms generate=%.0fms",
                standard, standard_hint, resolution.source, len(clauses), report.verdict,
                retrieve_ms, generate_ms)

    result["meta"] = {
        "standard": standard, "retrieved": len(clauses), "retrieve_ms": round(retrieve_ms),
        "generate_ms": round(generate_ms), "standard_resolution": resolution.as_meta(),
        "guard": report.as_meta(), "search_meta": search_meta,
    }
    return result
