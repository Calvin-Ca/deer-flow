"""
阶段 2.10：D 组合并

将 C 组 + D1（跨条文）+ D2（拒答）合并为 group_d/train.jsonl。
更新各样本的 group 字段为 "d"，保留原始 sample_type。

运行：python -m src.synth.group_d_merge
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
import sys
sys.path.insert(0, str(_ROOT))
from src.utils.fingerprint import clauses_fingerprint
_SOURCES = {
    "c":  _ROOT / "data/processed/group_c/train.jsonl",
    "d1": _ROOT / "data/processed/group_d1/train.jsonl",
    "d2": _ROOT / "data/processed/group_d2/train.jsonl",
}
_OUT_DIR = _ROOT / "data/processed/group_d"


def merge() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _OUT_DIR / "train.jsonl"

    counts: dict[str, int] = {}
    total = 0

    with open(out_file, "w", encoding="utf-8") as fout:
        for source_key, src_path in _SOURCES.items():
            if not src_path.exists():
                print(f"[group_d_merge] ⚠️  {src_path} 不存在，跳过")
                continue
            n = 0
            with open(src_path, encoding="utf-8") as f:
                for line in f:
                    s = json.loads(line)
                    s["group"] = "d"
                    fout.write(json.dumps(s, ensure_ascii=False) + "\n")
                    n += 1
            counts[source_key] = n
            total += n
            print(f"[group_d_merge] {source_key}: {n} 条")

    print(f"[group_d_merge] 合并完成：{total} 条 → {out_file}")

    manifest = {
        "group": "d",
        "version": "v1",
        "clauses_fingerprint": clauses_fingerprint(),
        "total": total,
        "source_counts": counts,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[group_d_merge] manifest → {_OUT_DIR / 'manifest.json'}")


if __name__ == "__main__":
    merge()
