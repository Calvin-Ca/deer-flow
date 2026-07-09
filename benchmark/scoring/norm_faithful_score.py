"""norm_faithful（L6-C 规范问答忠实度）打分器 —— 纯函数，供 traditional↔agentic 对比 runner 用。

RAGAS 式，但只做**能程序化判定**的四项（论断落地那半走 LLM 裁判，不在此）：
- **忠实率 faithfulness**：答案引的条款号是否真在检索证据里（复用 ``app.ce.norm.faithfulness.check_faithfulness``，
  即 agentic RAG 招牌）——治幻觉引用；只对"应作答"的用例算（拒答用例无引用正常）。
- **拒答正确性**：expect_refuse 与实际是否一致 → 误拒率（该答却拒）/ 漏拒率（该拒却答，最危险）。
- **答案要点覆盖 answer_points**：gold_answer_points 事实点在答案里的命中率（字符二元组召回≥阈值算命中，容许改写）。
- **std 级上下文召回 context_recall**：gold_contexts 的标准号是否被检索回来（clause 是占位名不可靠→只判 std 级，诚实）。

对比口径（MS.md ⑤ ablation）：同一集分别跑 traditional / agentic，比这四项 → 归因分解 vs 引用回查各自贡献。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 复用规范问答确定性忠实校验（单一实现，避免漂移）。app 在 backend/ 下、非安装包，
# 自包含地把 backend 加进 path 再 import（同 select_eval 注入思路），使本模块从任意 CWD 可导入。
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from app.ce.norm.faithfulness import check_faithfulness  # noqa: E402

_POINT_COVER_FLOOR = 0.5  # 答案要点命中阈值：点的字符二元组有一半出现在答案里即算覆盖（容改写）


@dataclass
class NormObs:
    """一次规范问答运行的外部观测（runner 从 DeerFlowClient 流里提取）。

    字段：answer 最终答案；evidence 检索回来的条文（list of dict，含 std/clause/node_path 等，供忠实回查与
      context_recall）；did_refuse 是否拒答（关键词判）。
    """

    answer: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    did_refuse: bool = False


_REFUSE_KW = ("未收录", "库内无", "无法给出", "无相关条文", "拒答", "超出", "不在收录", "建议查阅", "无库内依据")


def detect_refusal(answer: str | None) -> bool:
    """从答案文本判是否拒答（关键词）。"""
    return any(k in (answer or "") for k in _REFUSE_KW)


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s", "", str(s or ""))
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _point_covered(point: str, answer: str) -> bool:
    """事实点是否被答案覆盖（点的二元组在答案里的召回≥阈值，容许改写/换序）。"""
    P = _bigrams(point)
    if not P:
        return False
    A = _bigrams(answer)
    return len(P & A) / len(P) >= _POINT_COVER_FLOOR


def _norm_std(s: Any) -> str:
    return re.sub(r"[\s\-/]", "", str(s or "")).upper()


def _evidence_stds(evidence: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for e in evidence or []:
        if isinstance(e, dict):
            v = e.get("std") or e.get("standard") or e.get("standard_no")
            if v:
                out.add(_norm_std(v))
    return out


def answer_points_coverage(case: dict[str, Any], obs: NormObs) -> float | None:
    """gold_answer_points 事实点命中率（无点→None）。"""
    pts = case.get("gold_answer_points") or []
    if not pts:
        return None
    return sum(1 for p in pts if _point_covered(str(p), obs.answer)) / len(pts)


def context_recall_std(case: dict[str, Any], obs: NormObs) -> float | None:
    """gold_contexts 的标准号被检索回来的比例（std 级；clause 占位名不判，诚实）。证据无 std 字段→None。"""
    gold = case.get("gold_contexts") or []
    if not gold:
        return None
    ret = _evidence_stds(obs.evidence)
    if not ret:
        return None  # 证据没带 std 信息 → 不可靠判，标 None 而非算 0
    return sum(1 for g in gold if _norm_std(g.get("std")) in ret) / len(gold)


@dataclass
class CaseScore:
    case_id: str
    expect_refuse: bool
    refusal_ok: bool
    faithful_rate: float | None           # 仅应作答用例有意义
    unfaithful: bool                       # 有幻觉引用
    answer_points: float | None
    context_recall: float | None


def score_case(case: dict[str, Any], obs: NormObs) -> CaseScore:
    """判一条 norm_faithful 用例。"""
    expect_refuse = bool(case.get("expect_refuse"))
    faith = check_faithfulness(obs.answer, obs.evidence)
    return CaseScore(
        case_id=case.get("id", "?"),
        expect_refuse=expect_refuse,
        refusal_ok=(expect_refuse == obs.did_refuse),
        faithful_rate=(None if expect_refuse else faith["faithful_rate"]),
        unfaithful=(faith["verdict"] == "unfaithful"),
        answer_points=(None if expect_refuse else answer_points_coverage(case, obs)),
        context_recall=(None if expect_refuse else context_recall_std(case, obs)),
    )


def _mean(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


def aggregate(scores: list[CaseScore]) -> dict[str, Any]:
    """整套聚合：忠实率/幻觉率/答案要点/上下文召回（应答用例）+ 误拒率/漏拒率（拒答用例）。"""
    answer_cases = [s for s in scores if not s.expect_refuse]
    refuse_cases = [s for s in scores if s.expect_refuse]

    def _rate(subset, pred):
        return (sum(1 for s in subset if pred(s)) / len(subset)) if subset else None

    return {
        "n": len(scores),
        # 应作答用例：
        "faithful_rate": _mean([s.faithful_rate for s in answer_cases]),          # 引用命中率，越高越好
        "unfaithful_case_rate": _rate(answer_cases, lambda s: s.unfaithful),      # 有幻觉引用的用例占比，门=0
        "answer_points_coverage": _mean([s.answer_points for s in answer_cases]),
        "context_recall": _mean([s.context_recall for s in answer_cases]),
        # 拒答边界（全体分侧）：
        "false_refuse_rate": _rate(answer_cases, lambda s: not s.refusal_ok),     # 该答却拒
        "missed_refuse_rate": _rate(refuse_cases, lambda s: not s.refusal_ok),    # 该拒却答（最危险）
        "refusal_accuracy": _rate(scores, lambda s: s.refusal_ok),
    }
