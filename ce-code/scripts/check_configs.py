"""核查铁律 1：四组训练配置除数据外必须逐字相同。

铁律 1 原文——「四组消融除训练数据外，所有超参必须逐字相同。改任何超参必须
四组同步改，并在 docs/EXPERIMENT.md 记录变更」。这意味着它**不是一次性检查**：
每改一次 configs/ 都得重验，否则消融的自变量就不只是数据了。

同时核查铁律 2（固定 max_steps 不固定 epoch）与几项训练前必备设置——
这些都是「跑完才发现配错、几小时白烧」的类型，开跑前几秒钟能查完。

用法：
    python scripts/check_configs.py          # 退出码 0=通过，1=有问题
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_GROUPS = ("a", "b", "c", "d")

# 允许组间不同的键：指向各组自己的数据与产物。其余任何差异都违反铁律 1。
_PER_GROUP_KEYS = {"dataset", "dataset_dir", "output_dir", "run_name"}

# 必须存在且取值固定的键（铁律 2 与技术栈约束，见 CLAUDE.md §2）
_REQUIRED = {
    "max_steps": "1500",          # 铁律 2：固定步数，不固定 epoch
    "finetuning_type": "lora",
    "lora_rank": "32",
    "lora_alpha": "64",
    "lora_dropout": "0.05",
    "bf16": "true",
}


def _parse(path: Path) -> dict[str, str]:
    """解析 LLaMA-Factory 的扁平 yaml 配置。

    只取顶层 `key: value` 行，忽略注释与空值行——本项目的训练配置是扁平结构，
    引第三方 yaml 库徒增依赖。

    Args:
        path: yaml 路径

    Returns:
        键 → 值（均为字符串原文）
    """
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(\w+):\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def main() -> int:
    """核查四组配置并打印结果。

    Args:
        无

    Returns:
        退出码：0 通过，1 发现问题
    """
    cfgs: dict[str, dict[str, str]] = {}
    for g in _GROUPS:
        p = _ROOT / f"configs/group_{g}.yaml"
        if not p.exists():
            print(f"❌ 缺配置：{p.relative_to(_ROOT)}")
            return 1
        cfgs[g] = _parse(p)

    problems: list[str] = []

    # ── 铁律 1：除数据/产物路径外逐字相同 ──────────────────────────────
    keys = set().union(*(set(d) for d in cfgs.values()))
    mismatched: list[str] = []
    for k in sorted(keys - _PER_GROUP_KEYS):
        vals = {g: cfgs[g].get(k, "（缺）") for g in _GROUPS}
        if len(set(vals.values())) > 1:
            mismatched.append(k)
            problems.append(f"铁律 1：`{k}` 组间不一致 → " +
                            "  ".join(f"{g}={vals[g]}" for g in _GROUPS))

    print(f"铁律 1（四组超参逐字相同）：共 {len(keys)} 个键，"
          f"{len(_PER_GROUP_KEYS)} 个允许不同")
    print("  ✅ 通过" if not mismatched else f"  ❌ {len(mismatched)} 个键不一致")

    # 各组自己的路径必须确实指向自己，否则会拿错数据集训练
    for g in _GROUPS:
        for k in ("dataset", "dataset_dir", "output_dir", "run_name"):
            v = cfgs[g].get(k, "")
            if v and f"group_{g}" not in v:
                problems.append(f"{g} 组的 `{k}` = {v}，未指向 group_{g}——会拿错数据训练")

    # ── 铁律 2 与技术栈约束 ────────────────────────────────────────────
    print("\n铁律 2 与技术栈约束：")
    for k, expect in _REQUIRED.items():
        got = {g: cfgs[g].get(k, "（缺）") for g in _GROUPS}
        ok = all(v.lower() == expect for v in got.values())
        print(f"  {k:<18} 期望 {expect:<6} 实际 {got['a']:<8} {'✅' if ok else '❌'}")
        if not ok:
            problems.append(f"`{k}` 应为 {expect}，实际 " +
                            "  ".join(f"{g}={got[g]}" for g in _GROUPS))

    # num_train_epochs 与 max_steps 并存时，谁生效取决于框架实现——不该赌
    for g in _GROUPS:
        if "num_train_epochs" in cfgs[g] and "max_steps" in cfgs[g]:
            problems.append(
                f"{g} 组同时设了 num_train_epochs 与 max_steps——"
                f"哪个生效取决于框架实现，铁律 2 要求以步数为准，应删掉 epoch"
            )

    # ── 有效 batch size：四组必须相同，否则等效学习率不同 ──────────────
    print("\n有效 batch size（per_device × 累积 × 卡数=1）：")
    for g in _GROUPS:
        try:
            eff = int(cfgs[g]["per_device_train_batch_size"]) * \
                  int(cfgs[g]["gradient_accumulation_steps"])
        except (KeyError, ValueError):
            problems.append(f"{g} 组的 batch 相关字段缺失或非整数")
            continue
        print(f"  {g}: {cfgs[g]['per_device_train_batch_size']} × "
              f"{cfgs[g]['gradient_accumulation_steps']} = {eff}")

    # ── 数据文件是否就位 ───────────────────────────────────────────────
    #
    # train.jsonl 是 gitignore 的（体积大、可重新生成），只存在于实际生成它的机器上。
    # 故本地 Mac 上缺文件属正常，不该报成配置问题——否则每次在 Mac 上跑检查都
    # 满屏红字，真问题反而淹没。训练在服务器进行，那里才要求文件齐全。
    print("\n数据就位情况：")
    missing: list[str] = []
    for g in _GROUPS:
        f = _ROOT / f"data/processed/group_{g}/train.jsonl"
        info = _ROOT / f"data/processed/group_{g}/dataset_info.json"
        mf = _ROOT / f"data/processed/group_{g}/manifest.json"
        n = sum(1 for line in f.open(encoding="utf-8") if line.strip()) if f.exists() else 0
        fp = ""
        if mf.exists():
            import json
            v = json.loads(mf.read_text(encoding="utf-8")).get("clauses_fingerprint")
            fp = f"指纹 {v}" if v else "指纹 未记录"
        print(f"  {g}: {n:>6} 条  "
              f"train {'✅' if f.exists() else '—'}  "
              f"info {'✅' if info.exists() else '❌'}  {fp}")
        if not f.exists():
            missing.append(g)
        if not info.exists():
            problems.append(f"{g} 组缺 dataset_info.json（LLaMA-Factory 找不到数据集）")

    if missing:
        print(f"\n  ⓘ {','.join(missing)} 组本地无 train.jsonl。该文件 gitignore，"
              f"只存在于生成它的机器上；\n"
              f"    若这是训练机，说明数据确实没生成，训练会失败——请在训练机上复跑本检查确认。")

    # 指纹一致性：四组必须派生自同一份条文库，否则消融的自变量不只是合成策略。
    #
    # **缺指纹必须报问题，不能沉默放过**——原实现只在"已有指纹之间不一致"时告警，
    # 于是三组缺指纹、一组有指纹时 set 长度为 1，照样打绿灯。那正是本项目一直在
    # 修的同一类错误：把"没验证"当成"验证通过"。红线检查宁可说不知道。
    import json as _json
    fps: dict[str, str] = {}
    unverified: list[str] = []
    for g in _GROUPS:
        mf = _ROOT / f"data/processed/group_{g}/manifest.json"
        if not mf.exists():
            unverified.append(f"{g}（无 manifest）")
            continue
        v = _json.loads(mf.read_text(encoding="utf-8")).get("clauses_fingerprint")
        if v:
            fps[g] = v
        else:
            unverified.append(g)

    if len(set(fps.values())) > 1:
        problems.append(
            f"四组的 clauses_fingerprint 不一致——存在旧库产物，"
            f"消融的自变量不只是合成策略：{fps}"
        )
    if unverified:
        problems.append(
            f"{'、'.join(unverified)} 组未记录 clauses_fingerprint，无法确认是否派生自"
            f"当前条文库。跑 scripts/backfill_fingerprint.py 验证并回填，或重建该组"
        )

    print("\n" + "─" * 60)
    if problems:
        print(f"❌ 发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("✅ 全部通过，配置可用于训练")
    return 0


if __name__ == "__main__":
    sys.exit(main())
