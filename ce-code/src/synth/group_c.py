"""
阶段 2.7：Group C — B 组 + 三重质量过滤

串联：可答性 → 条款准确性 → 多样性去重
输出：data/processed/group_c/train.jsonl

运行：
  python -m src.synth.group_c
  python -m src.synth.group_c --workers 4   # 可答性过滤并发
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_GROUP_B = _ROOT / "data/processed/group_b/train.jsonl"
_OUT_DIR = _ROOT / "data/processed/group_c"
_TMP_DIR = _ROOT / "data/interim/filter_tmp"
_REJECTED_DIR = _ROOT / "data/interim/filtered_out"


def run(workers: int = 1, seed: int = 42) -> None:
    """串联三道过滤器，由 B 组产出 C 组。

    Args:
        workers: 可答性过滤的并发线程数（LLM 调用）
        seed:    去重的组内洗牌种子（铁律 7，记入 manifest）

    Returns:
        None（结果写入 data/processed/group_c/）
    """
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _REJECTED_DIR.mkdir(parents=True, exist_ok=True)

    if not _GROUP_B.exists():
        raise FileNotFoundError(f"B 组数据不存在：{_GROUP_B}，请先运行 group_b.py")

    # ── 统计起点 ──────────────────────────────────────────────────────────
    total_in = sum(1 for _ in open(_GROUP_B, encoding="utf-8"))
    print(f"[group_c] 输入：{total_in} 条（来自 group_b）")

    # ── 过滤器 1：可答性 ──────────────────────────────────────────────────
    print("\n[group_c] ① 可答性过滤...")
    tmp_answerable = _TMP_DIR / "after_answerable.jsonl"
    rej_answerable = _REJECTED_DIR / "rejected_answerable.jsonl"

    # 可答性过滤支持并发（API 调用）
    import sys
    sys.path.insert(0, str(_ROOT))

    from src.filter.answerable import filter_answerable
    filter_answerable(
        input_path=_GROUP_B,
        output_path=tmp_answerable,
        rejected_path=rej_answerable,
        clauses_path=_CLAUSES,
        workers=workers,
    )

    kept_1 = sum(1 for _ in open(tmp_answerable, encoding="utf-8"))
    rej_1 = total_in - kept_1

    # ── 过滤器 2：条款准确性 ──────────────────────────────────────────────
    print("\n[group_c] ② 条款准确性过滤...")
    tmp_clause = _TMP_DIR / "after_clause.jsonl"
    rej_clause = _REJECTED_DIR / "rejected_clause.jsonl"

    from src.filter.clause_check import filter_clause_check
    filter_clause_check(
        input_path=tmp_answerable,
        output_path=tmp_clause,
        rejected_path=rej_clause,
        clauses_path=_CLAUSES,
    )

    kept_2 = sum(1 for _ in open(tmp_clause, encoding="utf-8"))
    rej_2 = kept_1 - kept_2

    # ── 过滤器 3：多样性去重 ──────────────────────────────────────────────
    print("\n[group_c] ③ 多样性去重...")
    out_file = _OUT_DIR / "train.jsonl"
    rej_dedup = _REJECTED_DIR / "rejected_dedup.jsonl"

    from src.filter.dedup import filter_dedup
    filter_dedup(
        input_path=tmp_clause,
        output_path=out_file,
        rejected_path=rej_dedup,
        seed=seed,
    )

    kept_3 = sum(1 for _ in open(out_file, encoding="utf-8"))
    rej_3 = kept_2 - kept_3

    # ── 汇总 ─────────────────────────────────────────────────────────────
    total_rejected = total_in - kept_3
    reject_rate = total_rejected / total_in if total_in else 0
    print(f"""
[group_c] 过滤完成：
  输入：{total_in}
  ① 可答性淘汰：{rej_1}（{rej_1/total_in:.1%}）
  ② 条款准确性淘汰：{rej_2}（{rej_2/total_in:.1%}）
  ③ 多样性淘汰：{rej_3}（{rej_3/total_in:.1%}）
  ─────────────────────────────
  总淘汰：{total_rejected}（{reject_rate:.1%}）
  最终保留：{kept_3}
""")

    if reject_rate < 0.15:
        print("⚠️  淘汰率 < 15%，过滤器可能过于宽松，建议检查")
    if reject_rate > 0.65:
        print("⚠️  淘汰率 > 65%，过滤器可能过于严格，建议检查")

    # ── manifest ─────────────────────────────────────────────────────────
    manifest = {
        "group": "c",
        "version": "v1",
        "total": kept_3,
        "source": str(_GROUP_B),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dedup_seed": seed,
        "filter_stats": {
            "input": total_in,
            "rejected_answerable": rej_1,
            "rejected_clause": rej_2,
            "rejected_dedup": rej_3,
            "total_rejected": total_rejected,
            "reject_rate": round(reject_rate, 4),
        },
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[group_c] → {out_file}（{kept_3} 条）")
    print(f"[group_c] manifest → {_OUT_DIR / 'manifest.json'}")

    # 清理临时文件
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=1, help="可答性过滤并发线程数")
    parser.add_argument("--seed", type=int, default=42, help="去重洗牌种子（铁律 7）")
    args = parser.parse_args()
    run(workers=args.workers, seed=args.seed)
