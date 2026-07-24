"""
四组训练 yaml 一致性校验（阶段 4.2）。

用法：python -m src.utils.check_configs
规则：除 dataset / output_dir 字段外，四份 yaml 的所有值必须逐字相同。
      任何差异均报错，返回非零退出码。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("请先 pip install pyyaml", file=sys.stderr)
    sys.exit(1)

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"
GROUPS = ["group_a", "group_b", "group_c", "group_d"]

# 允许各组不同的字段（业务上本就不同）
ALLOWED_DIFFER = {"dataset", "dataset_dir", "output_dir", "run_name"}


def _load(group: str) -> dict:
    path = CONFIGS_DIR / f"{group}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def check() -> bool:
    configs = {g: _flatten(_load(g)) for g in GROUPS}
    all_keys: set[str] = set()
    for cfg in configs.values():
        all_keys.update(cfg.keys())
    all_keys -= ALLOWED_DIFFER

    errors: list[str] = []
    for key in sorted(all_keys):
        values = {g: configs[g].get(key, "<missing>") for g in GROUPS}
        unique = set(str(v) for v in values.values())
        if len(unique) > 1:
            errors.append(f"  字段 '{key}' 在各组不一致：{values}")

    if errors:
        print("❌ 配置不一致，实验前必须修复：")
        for e in errors:
            print(e)
        return False

    print(f"✅ 四组配置一致（已忽略字段：{sorted(ALLOWED_DIFFER)}）")
    return True


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
