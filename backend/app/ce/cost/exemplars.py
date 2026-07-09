"""选码主动学习闭环（few-shot 版，不训练）—— 采集 HITL 纠正 → 存 → 检索相似 → 注入 few-shot。

**核心洞察**：置信门本就是 active-learning 采样器（把低置信选码路由给人）。本模块只做"闭环"那半：
把人工在闸上给的正确码 log 下来，下次遇相似构件时**检索最相似的历史纠正当 few-shot 示例**塞进选码
上下文——同样的错不再犯，且**不重训、不要 GPU**。

四段：
- **采集** ``record_bill_correction``：HITL select_bill 闸 resume 时调，记 {构件, 候选, 模型选码, 人工最终码}；
- **存储** ``CorrectionStore``：append-only JSONL（env ``CE_CORRECTIONS_PATH`` 可配）；
- **检索** ``retrieve_exemplars``：按构件描述相似度取 top-k，**按 spec 版本隔离**（2013 纠正不喂 2024 选码）；
- **注入** ``format_fewshot`` / ``cost_recall_exemplars`` 工具：渲染成可塞进 prompt 的示例。

生产可把检索换成 ce-rag/embedding（此处用轻量字符二元组 Jaccard，无依赖、可单测；相似度接口不变）。
度量靠 benchmark(eval①)：回灌后 pass^k/Top-1 涨没涨、人工纠正率降没降——**任何回灌须过 eval 门禁防退化**。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langchain.tools import tool

_DEFAULT_PATH = Path(__file__).resolve().parents[3] / ".deer-flow" / "ce_corrections.jsonl"
_SIM_FLOOR = 0.12  # 相似度下限：太不像的历史纠正不当示例（避免注入噪声）


@dataclass
class Correction:
    """一条人工纠正（选码 active-learning 样本）。

    字段：feature 构件/做法描述；correct_code 人工最终确认的 9 位清单码；model_code 模型原本会选的码
      （用于区分"纠正"vs"确认"，可空）；region/spec 口径（检索按 spec 隔离）；verified 是否经复核
      （②cost-critic 复核过的纠正更可信，检索可优先）；source 来源标记。
    """

    feature: str
    correct_code: str
    model_code: str | None = None
    region: str | None = None
    spec: str | None = None
    verified: bool = True          # 人工 override 即视为可信真值；critic 复核过的也置 True
    source: str = "hitl"
    candidates: list[str] = field(default_factory=list)

    @property
    def was_correction(self) -> bool:
        """人工最终码 != 模型原选码 = 真纠正（模型确实错了）；相等 = 确认。"""
        return bool(self.model_code) and self.model_code != self.correct_code


def _norm(s: str | None) -> str:
    return re.sub(r"\s", "", str(s or ""))


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def similarity(a: str, b: str) -> float:
    """构件描述相似度（字符二元组 Jaccard，中文友好、无依赖）。0~1。"""
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / len(A | B) if (A or B) else 0.0


class CorrectionStore:
    """纠正样本存储（append-only JSONL）。"""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.environ.get("CE_CORRECTIONS_PATH", _DEFAULT_PATH))

    def add(self, c: Correction) -> None:
        """追加一条纠正（建目录、原子追加一行 JSON）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    def load(self) -> list[Correction]:
        """读全部纠正（文件不存在 → 空）；坏行跳过、不炸。"""
        if not self.path.exists():
            return []
        out: list[Correction] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Correction(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out


def retrieve_exemplars(feature: str, *, spec: str | None = None, k: int = 3,
                       store: CorrectionStore | None = None,
                       corrections: list[Correction] | None = None) -> list[Correction]:
    """检索与 feature 最相似的 top-k 历史纠正（**按 spec 版本隔离**，相似度过下限）。

    参数：feature 当前构件描述；spec 版本（给定则只取同 spec 的纠正，防 2013/2024 串味）；k 返回条数；
      store/corrections 二选一（测试可直接传 corrections 免落盘）。
    返回：按相似度降序的 Correction 列表（verified 优先、相似度次之），只保留相似度≥下限的。
    """
    pool = corrections if corrections is not None else (store or CorrectionStore()).load()
    if spec is not None:
        pool = [c for c in pool if c.spec is None or str(c.spec) == str(spec)]
    scored = [(similarity(feature, c.feature), c) for c in pool]
    scored = [(s, c) for s, c in scored if s >= _SIM_FLOOR]
    # verified 优先、相似度次之（复核过的纠正更该被当示例）
    scored.sort(key=lambda t: (t[1].verified, t[0]), reverse=True)
    return [c for _, c in scored[:k]]


def format_fewshot(exemplars: list[Correction]) -> str:
    """把纠正渲染成可塞进选码 prompt 的 few-shot 示例块；空则返回空串。"""
    if not exemplars:
        return ""
    lines = ["【历史人工选码示例（同口径，供参考，仍以当前候选为准）】"]
    for c in exemplars:
        tail = f"（曾误选 {c.model_code}）" if c.was_correction else ""
        lines.append(f"- 构件「{c.feature}」→ 正确清单码 {c.correct_code}{tail}")
    return "\n".join(lines)


def _top_candidate_code(candidates: list[dict[str, Any]]) -> str | None:
    """从候选里取模型原本会选的码（分最高的第一条），用于判纠正 vs 确认。"""
    for cand in candidates or []:
        if isinstance(cand, dict):
            code = cand.get("code") or cand.get("bill_code") or (cand.get("bill") or {}).get("code")
            if code:
                return str(code)
    return None


def record_bill_correction(feature: str | None, correct_code: str | None, *,
                           candidates: list[dict[str, Any]] | None = None,
                           model_code: str | None = None, region: str | None = None,
                           spec: str | None = None, verified: bool = True,
                           store: CorrectionStore | None = None) -> Correction | None:
    """采集一条 HITL 选码纠正入库（缺 feature/correct_code → 不记，返回 None）。

    在 select_bill 闸 resume（用户给了 selected_code）时调用。model_code 未给则从 candidates 推（top-1）。
    调用方须自行 try/except：采集失败绝不能拖垮组价流程。
    """
    if not feature or not correct_code:
        return None
    c = Correction(
        feature=str(feature), correct_code=str(correct_code).strip(),
        model_code=model_code or _top_candidate_code(candidates or []),
        region=region, spec=spec, verified=verified,
        candidates=[str(x) for x in (_candidate_codes(candidates or []))],
    )
    (store or CorrectionStore()).add(c)
    return c


def _candidate_codes(candidates: list[dict[str, Any]]) -> list[str]:
    out = []
    for cand in candidates or []:
        if isinstance(cand, dict):
            code = cand.get("code") or cand.get("bill_code") or (cand.get("bill") or {}).get("code")
            if code:
                out.append(str(code))
    return out


def cost_recall_exemplars(feature: str, spec: str | None = None, k: int = 3) -> dict[str, Any]:
    """检索历史人工选码纠正里与本构件最相似的示例（同 spec 口径），供选码前参考。

    主动学习闭环的"注入"端：把过去人工在 HITL 闸上纠正过的 {构件→正确码} 里最相似的几条拉回来当 few-shot，
    **仅供参考、仍须在当前候选内选码，不得据此编造候选外的码**。库为空时返回空示例。

    Args:
        feature: 当前构件/做法描述。
        spec: 版本口径（2013/2024）；给定则只取同 spec 的历史纠正（版本隔离）。
        k: 返回示例条数（默认 3）。
    """
    ex = retrieve_exemplars(feature, spec=spec, k=k)
    return {"count": len(ex), "fewshot": format_fewshot(ex),
            "exemplars": [{"feature": c.feature, "correct_code": c.correct_code,
                           "was_correction": c.was_correction} for c in ex]}


cost_recall_exemplars_tool = tool("cost_recall_exemplars", parse_docstring=True)(cost_recall_exemplars)

__all__ = [
    "Correction", "CorrectionStore", "similarity", "retrieve_exemplars", "format_fewshot",
    "record_bill_correction", "cost_recall_exemplars", "cost_recall_exemplars_tool",
]
