"""训练的可调试入口：在**本进程内**跑 run_exp()，不 fork torchrun。

为什么需要它：
    scripts/train.sh 走 `llamafactory-cli train`，而 llamafactory 的 launcher 在
    配了 deepspeed 时强制经 torchrun 启动（未设 FORCE_TORCHRUN 直接抛 ValueError）。
    torchrun 会 fork 子进程，断点打在父进程上完全不会命中，F5 调不了训练。

    llamafactory-cli 本身只是薄壳：launcher.launch() 内部要么 subprocess 起 torchrun，
    要么直接调 run_exp()。本脚本绕过外壳，走后者。

    与 train.sh **共用同一份 yaml**，超参、数据、DeepSpeed 配置完全一致，
    差别只在启动方式，故调试时看到的行为与生产训练一致。

⚠️ 仅供调试。正式训练一律用 ./scripts/train.sh，理由见其顶部注释
   （铁律 1 要求四组的启动环境逐字相同，脚本把三个环境变量固化了）。

用法：
    python scripts/train_debug.py                      # 默认 configs/group_a.yaml
    python scripts/train_debug.py configs/group_c.yaml
    VS Code 里选 "训练（可调试，单进程）" 配置按 F5
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[1]


def main() -> None:
    """在当前进程内启动一次训练。

    Args:
        无（从 sys.argv[1] 取配置文件路径，缺省 configs/group_a.yaml）

    Returns:
        None
    """
    cfg = sys.argv[1] if len(sys.argv) > 1 else "configs/group_a.yaml"
    cfg_path = (_ROOT / cfg).resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{cfg_path}")

    # 这批变量平时由 torchrun 注入。in-process 启动时必须手工补齐，
    # 否则 DeepSpeed 初始化分布式进程组时拿不到 rank / world_size 会失败。
    os.environ.setdefault("RANK", "0")
    os.environ.setdefault("LOCAL_RANK", "0")
    os.environ.setdefault("WORLD_SIZE", "1")
    os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
    os.environ.setdefault("MASTER_PORT", "29500")

    # 与 train.sh 保持一致的三项（说明见该脚本顶部）
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    os.environ.setdefault("NCCL_P2P_DISABLE", "1")
    os.environ.setdefault("NCCL_IB_DISABLE", "1")
    # 已在本进程内，无需再经 torchrun；置位以通过 llamafactory 的启动方式校验
    os.environ.setdefault("FORCE_TORCHRUN", "1")

    print("=" * 58)
    print("  训练调试入口（单进程，可断点）")
    print(f"  配置      : {cfg_path}")
    print(f"  可见 GPU  : {os.environ['CUDA_VISIBLE_DEVICES']}")
    print("  ⚠️ 仅供调试；正式训练请用 ./scripts/train.sh")
    print("=" * 58)

    # llamafactory 通过 sys.argv 读取 yaml 路径
    sys.argv = [sys.argv[0], str(cfg_path)]

    from llamafactory.train.tuner import run_exp
    run_exp()


if __name__ == "__main__":
    main()
