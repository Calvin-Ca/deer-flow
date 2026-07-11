"""清单选码核实器（verify_bill_code）—— lead 直调的确定性核实工具。

对应 CLAUDE.md §1 能力 2：给定项目特征 + 待核清单编码，核实「编码对不对、特征少没少」。
与 ``verify.py``（组价结果复核，纯函数无 I/O）分工不同：本模块**查真值**——
``ce-db_bill_get`` 取该编码的清单规范条目（含特征项 schema），``ce-rag_match_bill_item``
按特征描述召回候选核对编码命中。规则能定死的（格式/存在性/特征项 diff/召回命中）在此判，
语义贴切度不判（那是 cost-agent / cost-critic 的 LLM 那半）。

verdict 口径与 ``verify.py`` 一致：任一 critical → fail；无 critical 有 warn → doubt；全清 → pass。
"""
from __future__ import annotations

import re
from typing import Any

from langchain.tools import tool

from .mcp import call_mcp_tool
from .state import normalize_spec

_CODE_RE = re.compile(r"^(\d{9})(?:\d{3})?$")  # 9 位全国码，或 12 位含顺序号（取前 9）


def _finding(ftype: str, severity: str, detail: str) -> dict[str, Any]:
    """一条核实发现。severity: critical(硬错) / warn(存疑) / info。"""
    return {"type": ftype, "severity": severity, "detail": detail}


def _extract_feature_names(bill: Any) -> list[str]:
    """从 ce-db_bill_get 返回的清单条目里提取特征项名列表（防御式，兼容多种字段形态）。

    兼容：features / feature_items / required_features / 项目特征 键；条目为 str，或
    dict（取 name/feature/名称/title 之一）。找不到返回空列表（调用方降级为「schema 未知」）。
    """
    if not isinstance(bill, dict):
        return []
    for key in ("features", "feature_items", "required_features", "项目特征"):
        raw = bill.get(key)
        if not isinstance(raw, list):
            continue
        names: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                for name_key in ("name", "feature", "名称", "title"):
                    v = item.get(name_key)
                    if isinstance(v, str) and v.strip():
                        names.append(v.strip())
                        break
        if names:
            return names
    return []


def _unwrap_bill(result: Any) -> dict[str, Any] | None:
    """从 MCP 返回里剥出清单条目 dict（兼容 result/data/bill/item 一层嵌套）。"""
    if not isinstance(result, dict):
        return None
    for key in ("bill", "item", "data", "result"):
        inner = result.get(key)
        if isinstance(inner, dict) and inner:
            return inner
    return result if result else None


def _normalize_provided(provided_features: Any) -> list[str]:
    """归一用户已填特征项为名字列表（兼容 list[str] / list[dict] / dict）。"""
    if isinstance(provided_features, dict):
        return [str(k).strip() for k in provided_features if str(k).strip()]
    if isinstance(provided_features, list):
        names: list[str] = []
        for item in provided_features:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                for name_key in ("name", "feature", "名称"):
                    v = item.get(name_key)
                    if isinstance(v, str) and v.strip():
                        names.append(v.strip())
                        break
        return names
    return []


def _candidate_code9(candidate: dict[str, Any]) -> str | None:
    """取候选的 9 位码（兼容 code/bill_code/编码 键，12 位截前 9）。"""
    for key in ("code", "bill_code", "编码"):
        v = candidate.get(key)
        if isinstance(v, str):
            m = _CODE_RE.match(v.strip())
            if m:
                return m.group(1)
    return None


def verify_bill_code(
    feature: str,
    code: str,
    spec: str | None = None,
    provided_features: list[str] | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """核实一条清单选码：编码是否合法存在、是否匹配项目特征、特征项有无遗漏。

    Deterministic checker for a bill-code choice. It validates code format, fetches
    the bill spec truth from ce-db, diffs the required feature schema against what
    the user provided, and cross-checks the code against ce-rag recall candidates.
    Use this when the user gives a project feature plus a candidate bill code and
    asks whether the code is right or whether features are missing. Not for picking
    a new code from scratch (delegate that instead).

    Args:
        feature: Project feature description of the component or construction method.
        code: Bill code to verify, 9 or 12 digits.
        spec: Bill standard version, 2013 or 2024. Defaults to Shenzhen 2013.
        provided_features: Feature item names the user has already filled in.
            If omitted, feature names are matched against the description text.
        top_k: Number of recall candidates for the cross-check.
    """
    findings: list[dict[str, Any]] = []
    normalized_spec = normalize_spec(spec)

    # 1) 编码格式（确定性，格式坏则后续真值/召回核对仍按描述做，但 verdict 已 fail）
    code9: str | None = None
    m = _CODE_RE.match(str(code or "").strip())
    if m:
        code9 = m.group(1)
    else:
        findings.append(_finding("code_format", "critical", f"清单编码 {code!r} 非合法 9/12 位码"))

    # 2) 真值查询：该编码在清单规范里是否存在 + 特征项 schema
    required_features: list[str] = []
    bill_truth: dict[str, Any] | None = None
    if code9:
        tool_result = call_mcp_tool("ce-db_bill_get", {"code": code9, "spec": normalized_spec})
        if tool_result.get("status") != "ok":
            # 服务不可用 ≠ 编码错——如实透传，不给 verdict 定罪（测量错与 agent 错要分开）
            return {
                "status": "blocked",
                "verdict": None,
                "findings": findings + [_finding("truth_unavailable", "warn", "ce-db 清单真值服务不可用，无法核实")],
                "error": tool_result,
            }
        bill_truth = _unwrap_bill(tool_result.get("result"))
        if not bill_truth or all(not bill_truth.get(k) for k in ("code", "name", "features", "项目特征")):
            findings.append(_finding("code_not_found", "critical", f"编码 {code9} 在清单规范（{normalized_spec}）中不存在"))
            bill_truth = None
        else:
            required_features = _extract_feature_names(bill_truth)

    # 3) 特征项 diff：规范要求的特征项 vs 用户已填（未给已填清单则按描述文本包含匹配，弱信号）
    missing_features: list[str] = []
    if required_features:
        provided = _normalize_provided(provided_features)
        if provided:
            provided_text = " ".join(provided)
            missing_features = [name for name in required_features if name not in provided and name not in provided_text]
        else:
            missing_features = [name for name in required_features if name not in str(feature or "")]
        for name in missing_features:
            findings.append(_finding("missing_feature", "warn", f"特征项「{name}」未填写（规范要求）"))

    # 4) 召回交叉核对：按特征描述召回候选，看待核编码是否在候选内、排第几
    recall: dict[str, Any] = {"checked": False}
    if code9 and str(feature or "").strip():
        match_result = call_mcp_tool(
            "ce-rag_match_bill_item",
            {"description": str(feature).strip(), "spec": normalized_spec, "top_k": int(top_k)},
        )
        if match_result.get("status") == "ok":
            raw = match_result.get("result")
            raw_candidates = raw.get("candidates") if isinstance(raw, dict) else raw
            candidates = [c for c in raw_candidates if isinstance(c, dict)] if isinstance(raw_candidates, list) else []
            codes = [_candidate_code9(c) for c in candidates]
            rank = codes.index(code9) + 1 if code9 in codes else None
            recall = {
                "checked": True,
                "hit": rank is not None,
                "rank": rank,
                "top_candidates": [{"code": c9, "name": cand.get("name")} for c9, cand in zip(codes, candidates) if c9][:5],
            }
            if rank is None:
                findings.append(_finding("recall_miss", "warn", f"按特征召回 top{top_k} 候选未含编码 {code9}，编码与特征可能不匹配"))
        else:
            recall = {"checked": False, "error": match_result}
            findings.append(_finding("recall_unavailable", "info", "ce-rag 召回服务不可用，跳过编码-特征交叉核对"))

    severities = {f["severity"] for f in findings}
    verdict = "fail" if "critical" in severities else ("doubt" if "warn" in severities else "pass")
    return {
        "status": "done",
        "verdict": verdict,
        "findings": findings,
        "code": code,
        "code9": code9,
        "spec": normalized_spec,
        "required_features": required_features,
        "missing_features": missing_features,
        "recall": recall,
        "provenance": {"truth": "ce-db_bill_get", "recall": "ce-rag_match_bill_item"},
    }


verify_bill_code_tool = tool("verify_bill_code", parse_docstring=True)(verify_bill_code)

__all__ = ["verify_bill_code", "verify_bill_code_tool"]
