"""「项目特征 ↔ 清单项」匹配引擎——选码与核实的共同底座（单源，无 LLM）。

CLAUDE.md §1 能力 2 的正反两面本质是同一个匹配问题：
- **选码**（``code=None``）：特征描述 → ``ce-rag_match_bill_item`` 召回候选 → 门限自动选定 /
  低置信转人工（need_review）；选定后顺带做特征项 diff（正向路径也提醒"少特征"）。
- **核实**（``code`` 给定）：待核编码 → ``ce-db_bill_get`` 真值存在性 + 特征项 diff +
  同一召回的 rank 交叉核对 → verdict。

此前两半各写一套（``bill_check.py`` / ``nodes.py``），候选归一、取码、召回调用、verdict 口径
重复实现。本模块单源沉淀，消费面全部薄壳化且契约不变：
- ``bill_check.bill_match`` —— lead 直调的双模工具（code 缺省=选码 / 给定=核实，2026-07-12 合并，
  原 ``verify_bill_code`` 单核实工具随之退役）；
- ``nodes.bill_match_node`` / ``nodes.select_bill_node`` —— workflow 节点（选码模式，HITL 契约不变）；
- ``nodes.py`` 其余节点复用候选归一/取分等纯函数。

选码模式内置 few-shot 纠正示例（2026-07-12）：``_match_select`` 自动检索历史人工纠正
（``exemplars.cost_recall_exemplars``，best-effort），附在返回的 ``exemplar_hints``——
主动学习闭环的注入端从「提示词求着调」变为引擎硬内置。

规则能定死的（格式/存在性/特征 diff/门限/rank）在此判；语义贴切度不判——那是
cost-agent / cost-critic 的 LLM 那半。verdict 口径与 ``verify.py`` 一致：任一 critical → fail；
无 critical 有 warn → doubt；全清 → pass。

MCP 依赖注入：取数函数带 ``call_tool`` 参数（``None`` 时回落本模块 ``call_mcp_tool``，调用时
从模块全局解析）；薄壳传入自己模块的 ``call_mcp_tool`` 引用，各自单测的 monkeypatch 均有效。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .mcp import call_mcp_tool
from .state import normalize_spec, unsupported_spec_error

CODE_RE = re.compile(r"^(\d{9})(?:\d{3})?$")  # 9 位全国码，或 12 位含顺序号（取前 9）
AUTO_SELECT_THRESHOLD = 0.86  # 选码模式：top1 分数达标才自动选定
AUTO_SELECT_MARGIN = 0.08  # 选码模式：top1 须与 top2 拉开的最小分差

_McpCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def finding(ftype: str, severity: str, detail: str) -> dict[str, Any]:
    """一条匹配/核实发现。severity: critical(硬错) / warn(存疑) / info。"""
    return {"type": ftype, "severity": severity, "detail": detail}


def verdict_from(findings: list[dict[str, Any]]) -> str:
    """统一 verdict 口径：任一 critical → fail；无 critical 有 warn → doubt；全清 → pass。"""
    severities = {f["severity"] for f in findings}
    return "fail" if "critical" in severities else ("doubt" if "warn" in severities else "pass")


# ---- 候选归一（原 nodes._as_candidates / bill_check 各一套，合并为超集）----------
def as_candidates(value: Any) -> list[dict[str, Any]]:
    """从服务返回里剥出候选 dict 列表（兼容 candidates/items/matches/results/suggestions 包裹或裸列表）。"""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("candidates", "items", "matches", "results", "suggestions"):
        raw = value.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


def candidate_score(candidate: dict[str, Any]) -> float | None:
    """取候选分数（兼容 score/confidence/rerank_score/similarity，字符串数值也收）。"""
    for key in ("score", "confidence", "rerank_score", "similarity"):
        value = candidate.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def candidate_code(candidate: dict[str, Any]) -> str | None:
    """取候选的原样编码（兼容 code/bill_code/item_code/编码 键，不截位）。"""
    for key in ("code", "bill_code", "item_code", "编码"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def code9_of(value: str | None) -> str | None:
    """归一到 9 位全国码（12 位截前 9；非法格式返回 None）。"""
    m = CODE_RE.match(str(value or "").strip())
    return m.group(1) if m else None


def candidate_code9(candidate: dict[str, Any]) -> str | None:
    """取候选的 9 位码（12 位截前 9；无合法码返回 None）。"""
    return code9_of(candidate_code(candidate))


def top_candidates(candidates: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return candidates[:limit]


# ---- 真值与特征 diff --------------------------------------------------------------
def extract_feature_names(bill: Any) -> list[str]:
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


def unwrap_bill(result: Any) -> dict[str, Any] | None:
    """从 MCP 返回里剥出清单条目 dict（兼容 result/data/bill/item 一层嵌套）。"""
    if not isinstance(result, dict):
        return None
    for key in ("bill", "item", "data", "result"):
        inner = result.get(key)
        if isinstance(inner, dict) and inner:
            return inner
    return result if result else None


def normalize_provided(provided_features: Any) -> list[str]:
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


def diff_features(required_features: list[str], feature: str | None, provided_features: Any = None) -> list[str]:
    """规范要求的特征项 vs 用户已填的 diff，返回缺失项。

    已填清单（provided_features）优先按名字匹配；未给已填清单则按描述文本包含匹配（弱信号）。
    """
    if not required_features:
        return []
    provided = normalize_provided(provided_features)
    if provided:
        provided_text = " ".join(provided)
        return [name for name in required_features if name not in provided and name not in provided_text]
    return [name for name in required_features if name not in str(feature or "")]


def fetch_bill_truth(code9: str, spec: str | None = None, *, call_tool: _McpCall | None = None) -> dict[str, Any]:
    """按 9 位码取清单规范真值（``ce-db_bill_get``）+ 特征项 schema。

    返回：``{status: ok|not_found|error, bill, required_features, error?}``；
      服务不可用 → status=error 且透传原始返回（服务不可用 ≠ 编码错，调用方不定罪）。
    """
    call = call_tool or call_mcp_tool
    tool_result = call("ce-db_bill_get", {"code": code9, "spec": normalize_spec(spec)})
    if tool_result.get("status") != "ok":
        return {"status": "error", "bill": None, "required_features": [], "error": tool_result}
    bill = unwrap_bill(tool_result.get("result"))
    if not bill or all(not bill.get(k) for k in ("code", "name", "features", "项目特征")):
        return {"status": "not_found", "bill": None, "required_features": []}
    return {"status": "ok", "bill": bill, "required_features": extract_feature_names(bill)}


def feature_gap_report(code: str | None, spec: str | None, feature: str | None, provided_features: Any = None, *, call_tool: _McpCall | None = None) -> dict[str, Any]:
    """对一个（已选定/待核）编码做特征项缺口检查（真值 schema diff），best-effort。

    返回：``{checked, required_features, missing_features}``；编码非法或真值不可得时
      checked=False（不阻断调用方主流程，选码结果照常返回）。
    """
    code9 = code9_of(code)
    if not code9:
        return {"checked": False, "required_features": [], "missing_features": []}
    truth = fetch_bill_truth(code9, spec, call_tool=call_tool)
    if truth["status"] != "ok":
        return {"checked": False, "required_features": [], "missing_features": []}
    required = truth["required_features"]
    return {"checked": True, "required_features": required, "missing_features": diff_features(required, feature, provided_features)}


# ---- 召回与选定 -------------------------------------------------------------------
def recall_candidates(description: str, spec: str | None = None, top_k: int = 10, code_prefixes: Any = None, *, call_tool: _McpCall | None = None) -> dict[str, Any]:
    """按特征描述召回清单候选（``ce-rag_match_bill_item``），候选已归一。

    返回：``{status: ok|error, candidates, result, arguments, error?}``；arguments 为实际
      发给 MCP 的入参（供 provenance 记录）。
    """
    call = call_tool or call_mcp_tool
    arguments: dict[str, Any] = {
        "description": str(description),
        "spec": normalize_spec(spec),
        "top_k": int(top_k),
    }
    if code_prefixes is not None:
        arguments["code_prefixes"] = code_prefixes
    tool_result = call("ce-rag_match_bill_item", arguments)
    if tool_result.get("status") != "ok":
        return {"status": "error", "candidates": [], "result": None, "arguments": arguments, "error": tool_result}
    result = tool_result.get("result")
    return {"status": "ok", "candidates": as_candidates(result), "result": result, "arguments": arguments}


def select_from_candidates(candidates: list[dict[str, Any]], threshold: float = AUTO_SELECT_THRESHOLD, margin: float = AUTO_SELECT_MARGIN) -> dict[str, Any]:
    """候选内门限选定（纯函数）：top1 分数 ≥ threshold 且与 top2 拉开 ≥ margin 才自动选。

    返回：``{decided, selected, selected_code, confidence, sorted_candidates}``；
      decided=False 表示低置信，调用方转 need_review / HITL，不硬选。
    """
    sorted_candidates = sorted(candidates, key=lambda candidate: candidate_score(candidate) or -1.0, reverse=True)
    if not sorted_candidates:
        return {"decided": False, "selected": None, "selected_code": None, "confidence": None, "sorted_candidates": []}
    top = sorted_candidates[0]
    top_score = candidate_score(top)
    second_score = candidate_score(sorted_candidates[1]) if len(sorted_candidates) > 1 else None
    enough_margin = second_score is None or (top_score is not None and top_score - second_score >= margin)
    top_code = candidate_code(top)
    decided = bool(top_code and top_score is not None and top_score >= threshold and enough_margin)
    return {
        "decided": decided,
        "selected": top if decided else None,
        "selected_code": top_code if decided else None,
        "confidence": top_score if decided else None,
        "sorted_candidates": sorted_candidates,
    }


# ---- 统一入口：双模 match_bill -----------------------------------------------------
def match_bill(
    feature: str,
    spec: str | None = None,
    code: str | None = None,
    provided_features: list[str] | None = None,
    top_k: int = 10,
    auto_select_threshold: float = AUTO_SELECT_THRESHOLD,
    auto_select_margin: float = AUTO_SELECT_MARGIN,
    *,
    call_tool: _McpCall | None = None,
) -> dict[str, Any]:
    """「特征 ↔ 清单项」匹配统一入口：``code`` 给定 = 核实模式，缺省 = 选码模式。

    核实模式返回 ``{mode, status, verdict, findings, recall, ...}``，
    选码模式返回 ``{mode, status: done|need_review|blocked, selected_code?, candidates, ...}``。
    两模式共享同一召回、真值、特征 diff 与 verdict 口径。
    spec 过 agent 面口径闸（默认仅 2013，见 ``state.unsupported_spec_error``）。
    """
    mode = "verify" if code is not None else "select"
    spec_err = unsupported_spec_error(spec)
    if spec_err:
        return {"mode": mode, **spec_err}
    if code is not None:
        return _match_verify(feature, spec, code, provided_features, top_k, call_tool=call_tool)
    return _match_select(feature, spec, provided_features, top_k, auto_select_threshold, auto_select_margin, call_tool=call_tool)


def _exemplar_hints(feature: str, spec: str | None) -> list[dict[str, Any]]:
    """检索历史人工选码纠正作 few-shot 提示（best-effort：库空/异常一律返回空,绝不拖垮选码）。"""
    try:
        from .exemplars import cost_recall_exemplars

        result = cost_recall_exemplars(str(feature or ""), spec=spec, k=3)
        hints = result.get("exemplars") if isinstance(result, dict) else None
        return hints if isinstance(hints, list) else []
    except Exception:  # noqa: BLE001 —— few-shot 是增强不是依赖
        return []


def _match_verify(feature: str, spec: str | None, code: str, provided_features: list[str] | None, top_k: int, *, call_tool: _McpCall | None) -> dict[str, Any]:
    """核实模式：格式 → 真值存在性 + 特征 schema → 特征 diff → 召回交叉核对 → verdict。"""
    findings: list[dict[str, Any]] = []
    normalized_spec = normalize_spec(spec)

    # 1) 编码格式（确定性，格式坏则后续真值/召回核对仍按描述做，但 verdict 已 fail）
    code9 = code9_of(code)
    if code9 is None:
        findings.append(finding("code_format", "critical", f"清单编码 {code!r} 非合法 9/12 位码"))

    # 2) 真值查询：该编码在清单规范里是否存在 + 特征项 schema
    required_features: list[str] = []
    if code9:
        truth = fetch_bill_truth(code9, normalized_spec, call_tool=call_tool)
        if truth["status"] == "error":
            # 服务不可用 ≠ 编码错——如实透传，不给 verdict 定罪（测量错与 agent 错要分开）
            return {
                "mode": "verify",
                "status": "blocked",
                "verdict": None,
                "findings": findings + [finding("truth_unavailable", "warn", "ce-db 清单真值服务不可用，无法核实")],
                "error": truth["error"],
            }
        if truth["status"] == "not_found":
            findings.append(finding("code_not_found", "critical", f"编码 {code9} 在清单规范（{normalized_spec}）中不存在"))
        else:
            required_features = truth["required_features"]

    # 3) 特征项 diff：规范要求的特征项 vs 用户已填（未给已填清单则按描述文本包含匹配，弱信号）
    missing_features = diff_features(required_features, feature, provided_features)
    for name in missing_features:
        findings.append(finding("missing_feature", "warn", f"特征项「{name}」未填写（规范要求）"))

    # 4) 召回交叉核对：按特征描述召回候选，看待核编码是否在候选内、排第几
    recall: dict[str, Any] = {"checked": False}
    if code9 and str(feature or "").strip():
        recalled = recall_candidates(str(feature).strip(), normalized_spec, top_k, call_tool=call_tool)
        if recalled["status"] == "ok":
            candidates = recalled["candidates"]
            codes = [candidate_code9(c) for c in candidates]
            rank = codes.index(code9) + 1 if code9 in codes else None
            recall = {
                "checked": True,
                "hit": rank is not None,
                "rank": rank,
                "top_candidates": [{"code": c9, "name": cand.get("name")} for c9, cand in zip(codes, candidates) if c9][:5],
            }
            if rank is None:
                findings.append(finding("recall_miss", "warn", f"按特征召回 top{top_k} 候选未含编码 {code9}，编码与特征可能不匹配"))
        else:
            recall = {"checked": False, "error": recalled["error"]}
            findings.append(finding("recall_unavailable", "info", "ce-rag 召回服务不可用，跳过编码-特征交叉核对"))

    return {
        "mode": "verify",
        "status": "done",
        "verdict": verdict_from(findings),
        "findings": findings,
        "code": code,
        "code9": code9,
        "spec": normalized_spec,
        "required_features": required_features,
        "missing_features": missing_features,
        "recall": recall,
        "provenance": {"truth": "ce-db_bill_get", "recall": "ce-rag_match_bill_item"},
    }


def _match_select(feature: str, spec: str | None, provided_features: list[str] | None, top_k: int, threshold: float, margin: float, *, call_tool: _McpCall | None) -> dict[str, Any]:
    """选码模式：召回 → 门限选定 → 选定后特征 diff；低置信/零召回 → need_review 不硬选。"""
    normalized_spec = normalize_spec(spec)
    recalled = recall_candidates(str(feature or "").strip(), normalized_spec, top_k, call_tool=call_tool)
    if recalled["status"] != "ok":
        return {"mode": "select", "status": "blocked", "spec": normalized_spec, "candidates": [], "error": recalled["error"]}

    candidates = recalled["candidates"]
    if not candidates:
        return {
            "mode": "select",
            "status": "need_review",
            "spec": normalized_spec,
            "candidates": [],
            "count": 0,
            "findings": [finding("no_candidates", "warn", "召回为空，无可选清单候选，需补充特征或人工输入编码")],
            "provenance": {"recall": "ce-rag_match_bill_item"},
        }

    decision = select_from_candidates(candidates, threshold=threshold, margin=margin)
    ranked = top_candidates(decision["sorted_candidates"])
    if not decision["decided"]:
        return {
            "mode": "select",
            "status": "need_review",
            "spec": normalized_spec,
            "candidates": ranked,
            "count": len(candidates),
            "findings": [finding("low_confidence", "warn", "候选置信度不足或分差不够，不自动选码，需人工复核")],
            "exemplar_hints": _exemplar_hints(feature, normalized_spec),
            "provenance": {"recall": "ce-rag_match_bill_item"},
        }

    gap = feature_gap_report(decision["selected_code"], normalized_spec, feature, provided_features, call_tool=call_tool)
    findings = [finding("missing_feature", "warn", f"特征项「{name}」未填写（规范要求）") for name in gap["missing_features"]]
    hints = _exemplar_hints(feature, normalized_spec)
    return {
        "mode": "select",
        "status": "done",
        "exemplar_hints": hints,
        "verdict": verdict_from(findings),
        "findings": findings,
        "spec": normalized_spec,
        "selected_code": decision["selected_code"],
        "selected": decision["selected"],
        "confidence": decision["confidence"],
        "selection_source": "auto_confident_candidate",
        "candidates": ranked,
        "required_features": gap["required_features"],
        "missing_features": gap["missing_features"],
        "provenance": {"truth": "ce-db_bill_get", "recall": "ce-rag_match_bill_item"},
    }


__all__ = [
    "AUTO_SELECT_MARGIN", "AUTO_SELECT_THRESHOLD", "CODE_RE",
    "as_candidates", "candidate_code", "candidate_code9", "candidate_score", "code9_of",
    "diff_features", "extract_feature_names", "feature_gap_report", "fetch_bill_truth",
    "finding", "match_bill", "normalize_provided", "recall_candidates",
    "select_from_candidates", "top_candidates", "unwrap_bill", "verdict_from",
]
