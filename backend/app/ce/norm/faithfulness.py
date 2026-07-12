"""规范问答·引用忠实性校验（agentic RAG 的招牌，确定性、纯函数、无 LLM）。

治 RAG 头号幻觉：弱 8B 会编一个听起来对、但根本没检索到的条文号。本模块**回查答案里引的每个条款号
是否真在检索证据里**，不在 = 幻觉引用。把红线"无依据不编条文"从 prompt 祈祷变成 **enforced check**。

用法两处（同一 core）：
- **eval runner**：从 trace 里拿 {答案, 检索证据} 直接算忠实率——**可靠度量**，不依赖弱模型自觉；
- **norm-qa 子智能体**：定稿前调 `verify_norm` 工具自查——**agentic 行为**，不忠实则剥引用/降级"无库内依据"。

只做**存在性回查**（条款号 ∈ 检索证据的条款号集）——确定性、可单测。更重的**论断落地**（每个论断是否
真能从所引条文推出）走 RAGAS LLM 裁判（`benchmark/L6_agent/norm_faithful/norm_faithfulness.md`），不在本模块。

开关：``CE_NORM_FAITHFULNESS_CHECK``（默认开）——传统 RAG 基线对比时置 0 关掉（见 MS.md ⑤ ablation）。
"""
from __future__ import annotations

import os
import re
from typing import Any

from langchain.tools import tool

# 条款号：至少含一个小数点（5.3.4 / 4.1），从而不误吞年份(2024)/标准号(50854)/纯数字。
_CLAUSE_RE = re.compile(r"\b\d+(?:\.\d+)+\b")


def faithfulness_enabled() -> bool:
    """引用忠实性校验开关（env ``CE_NORM_FAITHFULNESS_CHECK``，默认开）。传统 RAG 基线对比时置 0。"""
    return os.environ.get("CE_NORM_FAITHFULNESS_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")


def _uniq(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


def extract_cited_clauses(text: str | None) -> list[str]:
    """从答案文本抠出被引用的条款号（去重保序）。"""
    return _uniq(_CLAUSE_RE.findall(str(text or "")))


def evidence_clauses(evidence: list[dict[str, Any]] | None) -> set[str]:
    """检索证据里的条款号集合（**用结构化字段**：clause/clause_no/node_path/id，不扫自由文本避免误配）。"""
    out: set[str] = set()
    for e in evidence or []:
        if not isinstance(e, dict):
            continue
        for key in ("clause", "clause_no", "node_path", "id"):
            v = e.get(key)
            if v:
                out.update(_CLAUSE_RE.findall(str(v)))
    return out


def check_faithfulness(answer: str | None, evidence: list[dict[str, Any]] | None) -> dict[str, Any]:
    """回查答案引的条款号是否都在检索证据里。

    参数：answer 生成的答案文本；evidence 检索回来的条文（list of dict，含 clause/node_path 等结构化字段）。
    返回：``{verdict, cited, faithful, unfaithful, faithful_rate}``；
      verdict：``no_citation``（没引条款——事实性答案缺引用本身可疑，拒答场景则正常）/ ``faithful``（全部引用
      都在证据里）/ ``unfaithful``（有引用不在证据里 = 幻觉）。faithful_rate：命中率（无引用时 None）。
    """
    cited = extract_cited_clauses(answer)
    ev = evidence_clauses(evidence)
    faithful = [c for c in cited if c in ev]
    unfaithful = [c for c in cited if c not in ev]
    verdict = "no_citation" if not cited else ("faithful" if not unfaithful else "unfaithful")
    return {
        "verdict": verdict,
        "cited": cited,
        "faithful": faithful,
        "unfaithful": unfaithful,
        "faithful_rate": (len(faithful) / len(cited)) if cited else None,
    }


def verify_norm(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """回查规范问答答案里引的条款号是否真在检索证据里（引用忠实性校验，确定性、不调 LLM）。

    agentic RAG 的诚实闸：定稿前自查——``verdict=unfaithful`` 说明有幻觉引用（引了没检索到的条款号），
    应**剥掉该引用或降级为"无库内依据"**，绝不放行编造的条文号。``no_citation`` + 非拒答答案也可疑。

    Args:
        answer: 生成的答案文本（含条款号引用，如 "依据 GB 50854-2013 第 5.3.4 条…"）。
        evidence: 本次检索回来的条文列表，每条含结构化字段（clause / node_path 等）供回查。
    """
    return check_faithfulness(answer, evidence)


verify_norm_tool = tool("verify_norm", parse_docstring=True)(verify_norm)

__all__ = [
    "faithfulness_enabled", "extract_cited_clauses", "evidence_clauses",
    "check_faithfulness", "verify_norm", "verify_norm_tool",
]
