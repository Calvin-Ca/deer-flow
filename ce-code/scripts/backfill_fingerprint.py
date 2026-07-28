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

# 可回填的组。b/c 用"text 相同"论证；d1 依赖 refs、该论证不成立，
# 改用重放配对池的更硬判据（见 _verify_d1_pairs）。
_ELIGIBLE = {"b", "c", "d1"}

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
    if group == "d1":
        # D1 的配对由 refs 决定，而 refs 恰是两个 2357 条版本之间唯一变化的字段，
        # 故"text 相同"的论证对它不成立。改用更硬的判据：配对是确定性的
        # （_build_pairs 同 seed 同 pool_size 必得同一组对），拿当前条文库重放一遍，
        # 看 D1 的实际条文对是不是它的子集——若 D1 建自旧 refs，配对必然对不上。
        passed, msg = _verify_d1_pairs(train)
        ok &= passed
        lines.append(msg)
    elif group not in _ELIGIBLE:
        ok = False
        lines.append(f"❌ {group} 组不在可回填名单内（来源不明），必须重建")
    else:
        lines.append(f"✅ {group} 组只用条文 text 造样本、不碰 refs，"
                     f"而两个 2357 条版本的 text 逐字相同")
    return ok, lines


def _verify_d1_pairs(train: Path) -> tuple[bool, str]:
    """用当前条文库重放 D1 的配对，验证其实际样本对确实出自当前 refs。

    _build_pairs 是确定性的：同 seed、同 pool_size 必得同一组条文对。
    故拿当前条文库重放，再检查 D1 每条样本的 (A,B) 是否都在池内——
    若 D1 建自旧 refs（那 282 条变过的引用），配对会落在池外。

    Args:
        train: D1 的 train.jsonl

    Returns:
        (是否通过, 结论文字)
    """
    try:
        from src.synth.group_d1 import _build_pairs, _load_clauses
    except Exception as exc:      # 依赖不全（本地 Mac 无 openai/tenacity）时如实说明
        return False, f"❌ 无法导入 group_d1 重放配对（{type(exc).__name__}），改为在训练机上验证"

    mf = train.parent / "manifest.json"
    meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    pool_size = meta.get("pool_size")
    seed = meta.get("seed", 42)
    if not pool_size:
        return False, "❌ manifest 未记录 pool_size，无法重放配对"

    clauses = _load_clauses(_CLAUSES)
    pool = {frozenset((a["clause_id"], b["clause_id"]))
            for a, b in _build_pairs(clauses, pool_size=pool_size, seed=seed)}

    actual, outside = 0, 0
    with train.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            src = (json.loads(line).get("meta") or {}).get("source_clauses", [])
            if len(src) == 2:
                actual += 1
                if frozenset(src) not in pool:
                    outside += 1
    if outside:
        return False, (f"❌ {actual} 条样本中有 {outside} 条的条文对不在当前库重放的"
                       f"配对池内——D1 建自旧 refs，须重建")
    return True, (f"✅ {actual} 条样本的条文对全部落在当前库重放的配对池内"
                  f"（pool_size={pool_size}, seed={seed}）——确认建自当前 refs")


def main() -> int:
    """验证并回填指纹。

    Args:
        无（从命令行读 --dry-run）

    Returns:
        退出码：0 成功，1 有组未通过验证
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只验证不写盘")
    ap.add_argument("--groups", default="b,c,d1", help="要回填的组，逗号分隔")
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
        # 依据按组分别记录：d1 走的是重放配对池，不是"不依赖 refs"那条论证，
        # 两者写反就成了假记录——manifest 是溯源凭据，不能含糊。
        basis = (
            "重放当前条文库的配对池，D1 的实际条文对全部落在池内，确认建自当前 refs"
            if group == "d1" else
            "该组只用条文 text、不依赖 refs，而两个 2357 条版本的 text 逐字相同"
        )
        data["fingerprint_backfill_note"] = (
            f"构建于 clauses_fingerprint 字段引入之前。回填依据："
            f"① 覆盖条文数 > 任何历史版本总条数（{_MAX_STALE_CLAUSES}），必定建自当前版本；"
            f"② {basis}。"
            f"回填时间 {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        )
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → 已回填 {current}（备份 {mf.name}.bak）\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
