"""为 clauses_fingerprint 字段引入之前构建的组回填指纹——但先验证再回填。

B 组（9407 条）与 C 组（4727 条）建于该字段引入之前，manifest 里没有指纹。
重跑 B 需数小时、C 需二十分钟，只为一个元数据字段并不划算。

**但不能直接盖章**——那等于凭空断言"它就是新的"，与指纹要防的问题同性质。
本脚本先跑三条可验证的检查，全过才回填：

  ① 该组引用的 source_clauses 必须全部存在于当前条文库
  ② 该组覆盖的条文数必须与当前库规模相符，且**超过任何历史版本的条数**
     ——条文库历史为 1751 → 1653 → 2357 → 2357（后两版 text 逐字相同、
     只有 refs 变了 282 条）。B 覆盖 2352 条，1653 条的旧版根本产不出这个数，
     故 B 必定建自 2357 条那两版之一。
  ③ 该组是否依赖 refs——B/C 只用条文 text 造样本，完全不碰 refs；
     后两版 text 逐字相同，故对 B/C 而言二者等价。D1 依赖 refs，
     **不适用此论证**，故本脚本拒绝回填 d1。

回填时写 fingerprint_backfilled=true 与论证依据，不冒充构建时记录的指纹。

用法：
    python scripts/backfill_fingerprint.py --dry-run
    python scripts/backfill_fingerprint.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT))

from src.utils.fingerprint import clauses_fingerprint

_CLAUSES = _ROOT / "data/interim/clauses.jsonl"

# 只允许回填不依赖 refs 的组。d1 的配对完全由 refs 决定，而 refs 恰是两版之间
# 唯一变化的字段——对它无法用"text 相同"论证等价，必须重建。
_ELIGIBLE = {"b", "c"}

# 条文库历史各版本条数（git log 实测）。回填的核心论证之一是"覆盖数超过
# 任何旧版的总条数"，故需要这个上界。
_MAX_STALE_CLAUSES = 1751


def _verify(group: str) -> tuple[bool, list[str]]:
    """验证某组是否确实派生自当前条文库。

    Args:
        group: 组名（a/b/c/d/d1/d2）

    Returns:
        (是否通过, 逐条检查结论)
    """
    lines: list[str] = []
    train = _ROOT / f"data/processed/group_{group}/train.jsonl"
    if not train.exists():
        return False, [f"缺 train.jsonl：{train}"]

    valid = {json.loads(l)["clause_id"] for l in _CLAUSES.open(encoding="utf-8") if l.strip()}
    used: set[str] = set()
    n = 0
    with train.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                used.update((json.loads(line).get("meta") or {}).get("source_clauses", []))
                n += 1

    ok = True

    # ① 引用的条文必须都在当前库里
    unknown = used - valid
    if unknown:
        ok = False
        lines.append(f"❌ 引用了 {len(unknown)} 个当前库不存在的条文（如 {sorted(unknown)[:3]}）")
    else:
        lines.append(f"✅ {n} 条样本引用的 {len(used)} 个条文全部存在于当前库")

    # ② 覆盖数必须超过任何历史版本的总条数
    if len(used) <= _MAX_STALE_CLAUSES:
        ok = False
        lines.append(
            f"❌ 覆盖 {len(used)} 条 ≤ 历史版本最大条数 {_MAX_STALE_CLAUSES}，"
            f"无法排除它建自旧库"
        )
    else:
        lines.append(
            f"✅ 覆盖 {len(used)} 条 > 任何历史版本的总条数 {_MAX_STALE_CLAUSES}，"
            f"必定建自当前的 {len(valid)} 条版本"
        )

    # ③ 是否依赖 refs
    if group not in _ELIGIBLE:
        ok = False
        lines.append(f"❌ {group} 组不在可回填名单内（依赖 refs 或来源不明），必须重建")
    else:
        lines.append(f"✅ {group} 组只用条文 text 造样本、不碰 refs，"
                     f"而两个 2357 条版本的 text 逐字相同")
    return ok, lines


def main() -> int:
    """验证并回填指纹。

    Args:
        无（从命令行读 --dry-run）

    Returns:
        退出码：0 成功，1 有组未通过验证
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只验证不写盘")
    ap.add_argument("--groups", default="b,c", help="要回填的组，逗号分隔")
    args = ap.parse_args()

    current = clauses_fingerprint()
    print(f"当前条文库指纹：{current}\n")

    failed = False
    for group in [g.strip() for g in args.groups.split(",") if g.strip()]:
        mf = _ROOT / f"data/processed/group_{group}/manifest.json"
        print(f"── {group} 组 ──")
        if not mf.exists():
            print("  ❌ 缺 manifest.json\n")
            failed = True
            continue
        data = json.loads(mf.read_text(encoding="utf-8"))
        if data.get("clauses_fingerprint") == current:
            print("  ⓘ 已有当前指纹，跳过\n")
            continue

        ok, lines = _verify(group)
        for line in lines:
            print(f"  {line}")
        if not ok:
            print("  → 验证未通过，不回填\n")
            failed = True
            continue

        if args.dry_run:
            print(f"  → 验证通过，将回填 {current}（dry-run，未写盘）\n")
            continue

        shutil.copy2(mf, mf.with_suffix(".json.bak"))
        data["clauses_fingerprint"] = current
        # 明确标注这是事后回填而非构建时记录——两者的可信度不同，不该混为一谈
        data["fingerprint_backfilled"] = True
        data["fingerprint_backfill_note"] = (
            f"构建于 clauses_fingerprint 字段引入之前。回填依据："
            f"该组覆盖条文数超过任何历史版本总条数（>{_MAX_STALE_CLAUSES}），"
            f"必定建自当前版本；且该组只用条文 text、不依赖 refs。"
            f"回填时间 {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        )
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → 已回填 {current}（备份 {mf.name}.bak）\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
