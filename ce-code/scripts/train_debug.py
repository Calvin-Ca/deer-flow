"""训练的可调试入口：在**本进程内**跑 run_exp()，不 fork torchrun。

为什么需要它：
    scripts/train.sh 走 `llamafactory-cli train`，而 llamafactory 的 launcher 在
    配了 deepspeed 时强制经 torchrun 启动（未设 FORCE_TORCHRUN 直接抛 ValueError）。
    torchrun 会 fork 子进程，断点打在父进程上完全不会命中，F5 调不了训练。

    llamafactory-cli 本身只是薄壳：launcher.launch() 内部要么 subprocess 起 torchrun，
    要么直接调 run_exp()。本脚本绕过外壳，走后者。

    与 train.sh **共用同一份 yaml**，超参、数据、DeepSpeed 配置完全一致，
    差别只在启动方式，故调试时看到的行为与生产训练一致。

⚠️ 仅供调试，正式训练一律用 ./scripts/train.sh。
   原因**不是**本脚本设置得不够严格（两者设的环境变量相同），而是**启动路径不同**：
     train.sh        llamafactory-cli → torchrun → 子进程，分布式环境由 torchrun 注入
     train_debug.py  本进程直接 run_exp()，RANK/WORLD_SIZE 等由本脚本伪造
   两条路在 DeepSpeed 初始化与进程组建立上不保证等价。铁律 1 要求四组「除训练数据外
   所有条件逐字相同」，因此**四组必须统一走同一条路**——混用才是问题所在。
   实践上统一走 train.sh，因为那是生产路径。

用法：
    python scripts/train_debug.py                      # 默认 configs/group_a.yaml
    python scripts/train_debug.py configs/group_c.yaml
    CE_DEBUG_GPU=2 python scripts/train_debug.py       # 换一张卡调试
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
        无（命令行：第一个位置参数为 yaml 路径，缺省 configs/group_a.yaml；
            其余参数原样透传给 llamafactory，用于临时覆盖 yaml 里的设置）

    Returns:
        None
    """
    # 第一个位置参数是 yaml，其余原样透传给 llamafactory（其 CLI 参数优先于 yaml）。
    # 这样 launch.json 里可以直接写 --max_steps 20 做冒烟，而 yaml 一个字不动
    # ——临时改 yaml 再改回来违反铁律 1，且极易忘。
    argv = sys.argv[1:]
    cfg = argv[0] if argv and not argv[0].startswith("-") else "configs/group_a.yaml"
    extra = argv[1:] if argv and not argv[0].startswith("-") else argv
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

    # 与 train.sh 保持一致的三项（说明见该脚本顶部）。
    # 必须**无条件覆盖**，不能用 setdefault：若 shell 里已导出
    # CUDA_VISIBLE_DEVICES=0,1,2,3，setdefault 会原样继承那 4 张卡，
    # 有效 batch 从 2×8=16 变成 64 且全程不报错——正是 train.sh 要防的那个失效模式。
    os.environ["CUDA_VISIBLE_DEVICES"] = os.environ.get("CE_DEBUG_GPU", "0")
    os.environ["NCCL_P2P_DISABLE"] = "1"
    os.environ["NCCL_IB_DISABLE"] = "1"
    # 已在本进程内，无需再经 torchrun；置位以通过 llamafactory 的启动方式校验
    os.environ["FORCE_TORCHRUN"] = "1"

    print("=" * 58)
    print("  训练调试入口（单进程，可断点）")
    print(f"  配置          : {cfg_path}")
    print(f"  可见 GPU      : {os.environ['CUDA_VISIBLE_DEVICES']}"
          f"（单卡，有效 batch = 2×8 = 16）")
    print(f"  NCCL P2P/IB   : {os.environ['NCCL_P2P_DISABLE']}/{os.environ['NCCL_IB_DISABLE']}")
    print(f"  RANK/WORLD    : {os.environ['RANK']}/{os.environ['WORLD_SIZE']}")
    if extra:
        print(f"  覆盖参数      : {' '.join(extra)}")
    print("  ⚠️ 仅供调试。本入口与 train.sh 的**启动路径不同**"
          "（in-process vs torchrun），")
    print("     四组训练必须统一走 train.sh，混用会引入未受控变量（铁律 1）。")
    print("=" * 58)

    # llamafactory 通过 sys.argv 读取 yaml 路径与覆盖参数。
    # 原实现写死 [argv0, yaml]，会把 --max_steps 之类**静默丢弃**——
    # 于是「以为在跑 20 步冒烟、实际跑满 1500 步」，且全程不报错。
    sys.argv = [sys.argv[0], str(cfg_path), *extra]

    from llamafactory.train.tuner import run_exp
    run_exp()


if __name__ == "__main__":
    main()
