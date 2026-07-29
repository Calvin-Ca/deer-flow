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


def _parse_overrides(tokens: list[str]) -> dict:
    """把命令行覆盖项解析成 dict，供合并进 yaml 配置。

    支持两种写法（launch.json 里用哪种都行）：
        --max_steps 20          argparse 风格
        max_steps=20            OmegaConf 风格
        --overwrite_output_dir  无值的开关，视为 True

    值经 yaml.safe_load 转换类型："20"→20、"1.0e-4"→0.0001、"true"→True，
    而 "none" 仍是字符串 none（report_to 需要的正是字符串）。不做转换的话
    max_steps 会变成字符串 "20"，HfArgumentParser 不做二次转换，行为难以预料。

    Args:
        tokens: 命令行里 yaml 之后的全部参数

    Returns:
        覆盖项字典
    """
    import yaml

    out: dict = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" in tok and not tok.startswith("-"):
            key, _, raw = tok.partition("=")
            out[key] = yaml.safe_load(raw)
            i += 1
            continue
        if tok.startswith("--"):
            key = tok[2:]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            # 下一个 token 是另一个覆盖项（--xxx 或 k=v）时，本项是无值开关。
            # 只判断 startswith("--") 不够：混用两种语法时
            # `--overwrite_output_dir logging_steps=1` 会把后者吞成前者的值。
            is_next_an_option = nxt is None or nxt.startswith("--") or (
                "=" in nxt and not nxt.startswith("-")
            )
            if is_next_an_option:
                out[key] = True
                i += 1
            else:
                out[key] = yaml.safe_load(nxt)
                i += 2
            continue
        raise ValueError(f"无法解析的覆盖参数：{tok!r}（用 --key value 或 key=value）")
    return out


def main() -> None:
    """在当前进程内启动一次训练。

    Args:
        无（命令行：第一个位置参数为 yaml 路径，缺省 configs/group_a.yaml；
            其余参数原样透传给 llamafactory，用于临时覆盖 yaml 里的设置）

    Returns:
        None
    """
    # 第一个位置参数是 yaml，其余为覆盖项。
    # 覆盖项**不能靠透传 sys.argv 实现**：llamafactory 的 read_args 见到 yaml 后
    # 走 OmegaConf.from_cli 解析剩余参数，那要求 `key=value` 语法；传 argparse
    # 风格的 `--max_steps 20` 会让 "--max_steps" 与 "20" 各成一个 key，
    # 最终报 `Some keys are not used by the HfArgumentParser`。
    # 故改为自己读 yaml、在内存里合并，再把 dict 直接交给 run_exp(args=...)。
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
    # 有效 batch 从 1×16=16 变成 64 且全程不报错——正是 train.sh 要防的那个失效模式。
    os.environ["CUDA_VISIBLE_DEVICES"] = os.environ.get("CE_DEBUG_GPU", "0")
    os.environ["NCCL_P2P_DISABLE"] = "1"
    os.environ["NCCL_IB_DISABLE"] = "1"
    # 已在本进程内，无需再经 torchrun；置位以通过 llamafactory 的启动方式校验
    os.environ["FORCE_TORCHRUN"] = "1"

    print("=" * 58)
    print("  训练调试入口（单进程，可断点）")
    print(f"  配置          : {cfg_path}")
    print(f"  可见 GPU      : {os.environ['CUDA_VISIBLE_DEVICES']}"
          f"（单卡，有效 batch = 1×16 = 16）")
    print(f"  NCCL P2P/IB   : {os.environ['NCCL_P2P_DISABLE']}/{os.environ['NCCL_IB_DISABLE']}")
    print(f"  RANK/WORLD    : {os.environ['RANK']}/{os.environ['WORLD_SIZE']}")
    if extra:
        print(f"  覆盖参数      : {' '.join(extra)}")
    print("  ⚠️ 仅供调试。本入口与 train.sh 的**启动路径不同**"
          "（in-process vs torchrun），")
    print("     四组训练必须统一走 train.sh，混用会引入未受控变量（铁律 1）。")
    print("=" * 58)

    import yaml

    conf = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    overrides = _parse_overrides(extra)
    conf.update(overrides)

    # yaml 里的相对路径（dataset_dir / deepspeed / output_dir）以 ce-code 为基准，
    # 而调试器的 cwd 未必是它。转成绝对路径，避免"配置没问题却找不到文件"。
    for key in ("dataset_dir", "deepspeed", "output_dir"):
        val = conf.get(key)
        if isinstance(val, str) and not Path(val).is_absolute():
            conf[key] = str((_ROOT / val).resolve())

    from llamafactory.train.tuner import run_exp
    run_exp(args=conf)


if __name__ == "__main__":
    main()
