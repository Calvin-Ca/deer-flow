"""把某次行为回归 run 的逐条分数矩阵拉回终端（M4 归因工具）。

Langfuse UI 列表只给 score 平均值且列名不显——归因要逐条（哪条错、错在哪个维度、
comment 里的期望 vs 实际）。本脚本按 run 的 session_id 约定（``exp-{run}-{item_id}``）
逐条拉 trace scores，打印矩阵 + 按维度重算平均（顺带对账 UI 的 Ø 值到底是哪列）。

运行（服务器，需 LANGFUSE_* env）：
    uv run --project backend python benchmark/_shared/dump_run_scores.py --run-name m4-behavior-v1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402

DATASET_NAME = "agent-routing-eval"


def _answer_snippet(output: object, limit: int = 60) -> str:
    """从 trace 顶层 output 里挤出一段可读答复文本（归因用，够看清说了什么就行）。

    output 形态不定（str / 末消息 dict / 消息列表 / 整个 state dict），逐层试探取 content；
    全失败退回 str(output)。压空白截断到 limit。
    """
    obj = output
    if isinstance(obj, dict):
        msgs = obj.get("messages")
        if isinstance(msgs, list) and msgs:
            obj = msgs[-1]
        if isinstance(obj, dict):
            obj = obj.get("content", obj)
    if isinstance(obj, list):  # blocks 形态：拼 text 块
        obj = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in obj)
    text = " ".join(str(obj or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="拉回行为回归 run 的逐条分数矩阵")
    parser.add_argument("--run-name", required=True)
    args = parser.parse_args()

    client = require_langfuse()
    dataset = client.get_dataset(DATASET_NAME)

    rows: list[dict] = []
    for item in dataset.items:
        session_id = f"exp-{args.run_name}-{item.id}"
        traces = client.api.trace.list(session_id=session_id, limit=5).data
        if not traces:
            rows.append({"id": item.id, "route": None, "clarify": None, "comment": "(无 trace)"})
            continue
        trace = traces[0]
        scores: dict[str, float] = {}
        comments: list[str] = []
        # trace 详情接口自带 scores（GET /api/public/traces/{id}）——比猜 score 列表 API 名稳；
        # 首版用 score_v_2.get 猜名失败被 except 静默吞、全表空（又一例「兜底不出声」）。
        try:
            full = client.api.trace.get(trace.id)
            slist = getattr(full, "scores", None) or []
        except Exception as exc:  # noqa: BLE001
            slist = []
            comments.append(f"(拉分失败: {exc})")
        for s in slist:
            name = getattr(s, "name", None)
            if name in ("route_correct", "clarify_correct"):
                scores[name] = getattr(s, "value", None)
                c = getattr(s, "comment", None)
                if c and getattr(s, "value", 1) == 0:
                    comments.append(f"{name}: {c[:90]}")
        # 挂零分的条目补答复摘要——「route ✓ 但没问」类失败光看工具列表裁决不了（B2/E2/E3），
        # 得看 agent 到底说了什么（纯文本反问？直接作答？材料索要？）
        if any(v == 0 for v in scores.values()):
            snippet = _answer_snippet(getattr(full, "output", None)) if slist else ""
            if snippet:
                comments.append(f"答复: {snippet}")
        # 金标 id 靠 metadata（upload 时把原 id 放哪要看实际字段）——两处都试
        gold_id = (item.metadata or {}).get("id") or (item.metadata or {}).get("case_id") or item.id[:8]
        rows.append({"id": gold_id, "route": scores.get("route_correct"),
                     "clarify": scores.get("clarify_correct"),
                     "comment": " ｜ ".join(comments)})

    def fmt(v: object) -> str:
        return "—" if v is None else ("✓" if v else "✗")

    print(f"{'用例':<10}{'route':<7}{'clarify':<9}失败详情")
    print("-" * 80)
    for r in sorted(rows, key=lambda x: str(x["id"])):
        print(f"{str(r['id']):<10}{fmt(r['route']):<7}{fmt(r['clarify']):<9}{r['comment']}")

    for dim in ("route", "clarify"):
        vals = [r[dim] for r in rows if r[dim] is not None]
        if vals:
            print(f"\n{dim}_correct: {sum(vals)}/{len(vals)} = {sum(vals) / len(vals):.2%}"
                  f"（未挂分 {len(rows) - len(vals)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
