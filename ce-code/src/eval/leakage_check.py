"""
阶段 3.3：泄漏检查

将评测集每道题与四组训练数据（A/B/C/D）做向量相似度检查。
cosine > 0.9 的题目标记为泄漏，输出替换清单。

铁律：发现泄漏必须替换，不能删除（会破坏配额）。

运行：
  python -m src.eval.leakage_check
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_EVAL_FILE = _ROOT / "data/eval/evalset_v1.jsonl"
_TRAIN_FILES = {
    "a": _ROOT / "data/processed/group_a/train.jsonl",
    "b": _ROOT / "data/processed/group_b/train.jsonl",
    "c": _ROOT / "data/processed/group_c/train.jsonl",
    "d": _ROOT / "data/processed/group_d/train.jsonl",
}
_REPORT_PATH = _ROOT / "data/eval/leakage_report.md"
_THRESHOLD = 0.9


def run() -> None:
    print("[leakage_check] 加载 embedding 模型...")
    from sentence_transformers import SentenceTransformer
    import numpy as np

    model = SentenceTransformer("BAAI/bge-small-zh-v1.5")

    # 加载评测集
    eval_items: list[dict] = []
    with open(_EVAL_FILE, encoding="utf-8") as f:
        for line in f:
            eval_items.append(json.loads(line))
    eval_questions = [item["question"] for item in eval_items]
    print(f"[leakage_check] 评测集：{len(eval_items)} 题")

    print("[leakage_check] 编码评测集...")
    eval_embs = model.encode(eval_questions, normalize_embeddings=True, show_progress_bar=True)

    # 收集训练问题
    train_questions: list[tuple[str, str]] = []  # (group, question)
    for group, path in _TRAIN_FILES.items():
        if not path.exists():
            print(f"  ⚠️  {group} 组数据不存在，跳过")
            continue
        count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = json.loads(line)
                q = s["conversations"][0]["value"]
                train_questions.append((group, q))
                count += 1
        print(f"  {group} 组：{count} 条")

    print(f"[leakage_check] 训练集总计：{len(train_questions)} 条，编码中...")

    # 分批编码（避免 OOM）
    batch_size = 512
    train_embs_list = []
    for i in range(0, len(train_questions), batch_size):
        batch = [q for _, q in train_questions[i:i + batch_size]]
        embs = model.encode(batch, normalize_embeddings=True)
        train_embs_list.append(embs)
    import numpy as np
    train_embs = np.vstack(train_embs_list)

    # 相似度计算（eval × train 矩阵）
    print("[leakage_check] 计算相似度矩阵...")
    sims = eval_embs @ train_embs.T  # shape: (n_eval, n_train)

    leaks: list[dict] = []
    for i, item in enumerate(eval_items):
        max_sim = float(sims[i].max())
        max_j = int(sims[i].argmax())
        if max_sim >= _THRESHOLD:
            leaks.append(
                {
                    "eval_id": item["id"],
                    "eval_type": item["type"],
                    "eval_question": item["question"][:100],
                    "max_cosine": round(max_sim, 4),
                    "matched_group": train_questions[max_j][0],
                    "matched_question": train_questions[max_j][1][:100],
                }
            )

    # 写报告
    _write_report(leaks, len(eval_items), len(train_questions))
    print(f"\n[leakage_check] 泄漏 {len(leaks)}/{len(eval_items)} 题（阈值 {_THRESHOLD}）")
    print(f"[leakage_check] 报告 → {_REPORT_PATH}")

    if leaks:
        print("⚠️  以下题目需替换（见报告）：")
        for lk in leaks:
            print(f"  [{lk['eval_type']}] {lk['eval_id']}  cos={lk['max_cosine']}  matched={lk['matched_group']}")


def _write_report(leaks: list[dict], n_eval: int, n_train: int) -> None:
    _ROOT.joinpath("data/eval").mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 泄漏检查报告",
        f"",
        f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC",
        f"评测集：{n_eval} 题  训练集：{n_train} 条  阈值：{_THRESHOLD}",
        f"",
        f"## 结论",
        f"",
        f"**泄漏题数：{len(leaks)} / {n_eval}**",
        f"",
    ]
    if not leaks:
        lines.append("✅ 无泄漏，评测集可用。")
    else:
        lines += [
            "⚠️ 以下题目需替换重出（不得删除，配额必须保持）：",
            "",
            "| eval_id | 类型 | 相似度 | 命中组 | 评测题（截取） | 命中训练题（截取） |",
            "|---|---|---|---|---|---|",
        ]
        for lk in leaks:
            lines.append(
                f"| {lk['eval_id']} | {lk['eval_type']} | {lk['max_cosine']} "
                f"| {lk['matched_group']} | {lk['eval_question']} | {lk['matched_question']} |"
            )
        lines += [
            "",
            "## 操作步骤",
            "1. 对上表每道题，重新生成替换题（相同类型、相同难度）",
            "2. 替换后重新运行本脚本确认无泄漏",
            "3. 旧题保留在 evalset_v1_replaced.jsonl 作记录",
        ]

    _REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    run()
