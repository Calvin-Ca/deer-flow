"""一次性修复：把已生成数据里记错的 synth_model 改回真实合成模型。

背景——group_b.py 曾在三处各写一份模型名字面量：调用处是 `/models/Qwen3-32B-AWQ`，
而样本元数据与 manifest 却写着 `qwen-max`（早期方案残留，换模型时漏改）。
于是 9407 条样本的溯源信息全是假的：数据本身没问题，但"这批数据谁生成的"记错了。
源码已改为单一常量 `_SYNTH_MODEL`，本脚本负责回填存量数据。

为什么不重跑：重跑 B 组需数小时 GPU，而错的只是一个元数据字段，
样本内容逐字不变。改字段比重造数据既省又不引入新的随机性
（重跑会因 temperature=0.8 产出不同样本，反而破坏与 C/D 的派生关系）。

安全性：① 先备份 ② 只改 meta.synth_model 一个键，其余键与顺序原样保留
③ 幂等，重复运行无副作用 ④ 改完逐条复核并打印计数。

用法：
    python scripts/fix_synth_model_meta.py --dry-run   # 只看会改多少，不落盘
    python scripts/fix_synth_model_meta.py             # 实际修复
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[1]

# 错值 → 正确值。取自 group_b.py 的 _SYNTH_MODEL（调用处实际用的就是它）。
_WRONG = "qwen-max"
_RIGHT = "/models/Qwen3-32B-AWQ"

_TARGETS = [
    _ROOT / "data/processed/group_b/train.jsonl",
    # C/D 组由 B 派生，样本元数据会带着同一个错值传下去
    _ROOT / "data/processed/group_c/train.jsonl",
    _ROOT / "data/processed/group_d/train.jsonl",
]
_MANIFESTS = [
    _ROOT / "data/processed/group_b/manifest.json",
]


def _fix_jsonl(path: Path, dry_run: bool) -> tuple[int, int]:
    """修复单个 jsonl 里所有样本的 meta.synth_model。

    Args:
        path:    train.jsonl 路径
        dry_run: 为真时只统计不写盘

    Returns:
        (总条数, 被修改条数)
    """
    if not path.exists():
        print(f"  跳过（不存在）: {path.relative_to(_ROOT)}")
        return 0, 0

    rows, fixed = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            s = json.loads(line)
            meta = s.get("meta") or {}
            if meta.get("synth_model") == _WRONG:
                meta["synth_model"] = _RIGHT
                fixed += 1
            rows.append(s)

    rel = path.relative_to(_ROOT)
    if fixed and not dry_run:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as f:
            for s in rows:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"  {rel}: {len(rows)} 条中修复 {fixed} 条（备份 → {backup.name}）")
    elif fixed:
        print(f"  {rel}: {len(rows)} 条中待修复 {fixed} 条（dry-run，未写盘）")
    else:
        print(f"  {rel}: {len(rows)} 条，无需修复")
    return len(rows), fixed


def _fix_manifest(path: Path, dry_run: bool) -> bool:
    """修复 manifest.json 顶层的 synth_model 字段。

    Args:
        path:    manifest.json 路径
        dry_run: 为真时只报告不写盘

    Returns:
        是否发生（或将发生）修改
    """
    if not path.exists():
        print(f"  跳过（不存在）: {path.relative_to(_ROOT)}")
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("synth_model") != _WRONG:
        print(f"  {path.relative_to(_ROOT)}: synth_model={data.get('synth_model')!r}，无需修复")
        return False
    data["synth_model"] = _RIGHT
    data["meta_fixed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["meta_fix_note"] = f"synth_model 由 {_WRONG!r} 更正为实际调用的 {_RIGHT!r}"
    if not dry_run:
        shutil.copy2(path, path.with_suffix(".json.bak"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {path.relative_to(_ROOT)}: 已更正")
    else:
        print(f"  {path.relative_to(_ROOT)}: 待更正（dry-run）")
    return True


def _verify() -> None:
    """复核：确认目标文件里已不存在错值。

    Args:
        无

    Returns:
        None（结果打印到标准输出）
    """
    print("\n复核：")
    clean = True
    for path in _TARGETS:
        if not path.exists():
            continue
        left = sum(
            1 for line in open(path, encoding="utf-8")
            if line.strip() and (json.loads(line).get("meta") or {}).get("synth_model") == _WRONG
        )
        mark = "✅" if left == 0 else "❌"
        print(f"  {mark} {path.relative_to(_ROOT)}: 残留错值 {left} 条")
        clean &= left == 0
    for path in _MANIFESTS:
        if not path.exists():
            continue
        v = json.loads(path.read_text(encoding="utf-8")).get("synth_model")
        mark = "✅" if v != _WRONG else "❌"
        print(f"  {mark} {path.relative_to(_ROOT)}: synth_model={v!r}")
        clean &= v != _WRONG
    print("\n全部干净 ✅" if clean else "\n⚠️ 仍有残留，请检查")


def main() -> None:
    """入口：修复存量数据中记错的 synth_model 并复核。

    Args:
        无（从命令行读取 --dry-run）

    Returns:
        None
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args()

    print(f"修复 synth_model: {_WRONG!r} → {_RIGHT!r}\n")
    print("样本文件：")
    total = total_fixed = 0
    for path in _TARGETS:
        n, k = _fix_jsonl(path, args.dry_run)
        total += n
        total_fixed += k

    print("\nmanifest：")
    for path in _MANIFESTS:
        _fix_manifest(path, args.dry_run)

    print(f"\n合计 {total} 条样本，修复 {total_fixed} 条")
    if not args.dry_run:
        _verify()


if __name__ == "__main__":
    main()
