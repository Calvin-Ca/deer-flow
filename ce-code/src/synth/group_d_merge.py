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


def _check_sources() -> tuple[dict[str, str], list[str]]:
    """核对三个来源的 clauses_fingerprint 是否与当前条文库一致。

    **合并时直接盖当前指纹是假的保证**：D 由 C/D1/D2 拼成，若其中任一是旧库产物，
    合并出来的 D 照样带一个绿色指纹——恰恰是引入指纹要防的那类事。指纹必须
    反映来源的真实出处，而不是合并那一刻的条文库状态。

    Args:
        无

    Returns:
        (来源指纹表, 告警列表)
    """
    current = clauses_fingerprint()
    fps: dict[str, str] = {}
    warns: list[str] = []
    for key, path in _SOURCES.items():
        mf = path.parent / "manifest.json"
        if not mf.exists():
            warns.append(f"{key} 无 manifest，无法验证出处")
            continue
        fp = json.loads(mf.read_text(encoding="utf-8")).get("clauses_fingerprint")
        fps[key] = fp or "未记录"
        if not fp:
            warns.append(f"{key} 未记录 clauses_fingerprint（构建于该字段引入前），无法验证出处")
        elif fp != current:
            warns.append(f"{key} 指纹 {fp} ≠ 当前条文库 {current}——是旧库产物，须重建")
    return fps, warns


def merge() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _OUT_DIR / "train.jsonl"

    source_fps, warns = _check_sources()
    if warns:
        print("[group_d_merge] ⚠️ 来源出处校验：")
        for w in warns:
            print(f"    {w}")
    else:
        print("[group_d_merge] ✅ 三个来源均确认派生自当前条文库")

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
        # 只有三个来源全部确认同源，D 才配拥有指纹；否则如实置空——
        # 盖一个来路不明的绿灯比没有指纹更糟。
        "clauses_fingerprint": clauses_fingerprint() if not warns else None,
        "source_fingerprints": source_fps,
        "provenance_warnings": warns,
        "total": total,
        "source_counts": counts,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[group_d_merge] manifest → {_OUT_DIR / 'manifest.json'}")


if __name__ == "__main__":
    merge()
