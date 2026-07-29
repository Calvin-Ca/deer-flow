#!/usr/bin/env bash
#
# 四组消融训练的统一启动入口。
#
# 为什么必须走这个脚本而不是直接敲 llamafactory-cli：
#   铁律 1 要求四组除训练数据外所有条件逐字相同，而其中三项**不在 yaml 里**、
#   只能由环境变量控制。手敲命令时漏掉任何一个都不会报错，却会静默改变实验条件：
#
#   CUDA_VISIBLE_DEVICES=0  不设则 LLaMA-Factory 自动拉满 4 卡
#                           （torchrun --nproc_per_node 4），有效 batch
#                           从 1×16=16 变成 1×16×4=64。
#   NCCL_P2P_DISABLE=1      RTX 40 系消费卡被 NVIDIA 关闭了 P2P，
#   NCCL_IB_DISABLE=1       不设则 accelerate 抛 NotImplementedError 直接启动失败。
#   FORCE_TORCHRUN=1        yaml 里配了 deepspeed 时 LLaMA-Factory 强制要求经
#                           torchrun 启动，不设则报 ValueError 拒绝运行。
#                           因上面只暴露 1 张卡，torchrun 会以 nproc_per_node=1
#                           起单进程，有效 batch 仍为 16。
#
# 前置：基座权重需预先下载到本地（configs/group_*.yaml 的 model_name_or_path 指向它）。
#   hf-mirror 实测仅 12kB/s 且反复超时，改用 ModelScope：
#   uv run --with modelscope modelscope download --model Qwen/Qwen2.5-7B-Instruct \
#       --local_dir /mnt/nvme/calvin/models/Qwen2.5-7B-Instruct
#   下完应约 15GB，含 4 个 model-0000X-of-00004.safetensors 分片。
#
# 用法：
#   ./scripts/train.sh a          # 正式训练 group_a（1500 步）
#   ./scripts/train.sh a --smoke  # 冒烟：20 步、不落 checkpoint、不报 wandb
#
# --smoke 的存在理由：正式跑要几小时，而"显存不够 / DeepSpeed 起不来 /
#   ShareGPT 格式没被正确解析 / loss 不降"这几类问题在头 20 步就会暴露。
#   不提供这个开关，就只能临时改 yaml 里的 max_steps——那既违反铁律 1
#   （四组超参必须逐字相同），也极易改完忘了改回来。故做成命令行开关：
#   yaml 一个字不动，冒烟产物也不会污染正式的 checkpoints/。
#
# 输入：组别字母（a/b/c/d），对应 configs/group_<x>.yaml
# 输出：checkpoints/group_<x>/（LoRA adapter + 训练日志），wandb run 名为 group_<x>
#       --smoke 时输出到 checkpoints/_smoke_group_<x>/
set -euo pipefail

GROUP="${1:-}"
SMOKE="${2:-}"
if [[ ! "$GROUP" =~ ^[abcd]$ ]]; then
    echo "用法: $0 {a|b|c|d} [--smoke]" >&2
    exit 1
fi
if [[ -n "$SMOKE" && "$SMOKE" != "--smoke" ]]; then
    # ${SMOKE} 必须带花括号：中文全角「（」紧跟变量名时 bash 会把它并入变量名，
    # 解析成一个未定义的变量名，在 set -u 下直接报 unbound variable。
    echo "未知参数: ${SMOKE}（只支持 --smoke）" >&2
    exit 1
fi

CONFIG="configs/group_${GROUP}.yaml"
if [[ ! -f "$CONFIG" ]]; then
    echo "找不到配置文件: ${CONFIG}（请在 ce-code/ 目录下执行本脚本）" >&2
    exit 1
fi

# ── 四组必须完全一致的环境（改动须同步四组并记入 EXPERIMENT.md §6）──────────
export CUDA_VISIBLE_DEVICES=0
export NCCL_P2P_DISABLE=1
export NCCL_IB_DISABLE=1
export FORCE_TORCHRUN=1

# 冒烟模式的覆盖项走命令行传给 llamafactory-cli（其 CLI 参数优先于 yaml），
# yaml 本身一个字不动——铁律 1 要求四组配置逐字相同，临时改文件再改回来
# 是最容易出错的做法。
EXTRA=()
MODE="正式训练（1500 步）"
if [[ "$SMOKE" == "--smoke" ]]; then
    EXTRA=(
        --max_steps 20
        --output_dir "checkpoints/_smoke_group_${GROUP}"   # 不污染正式产物
        --save_steps 1000000                               # 20 步内不落 checkpoint
        --logging_steps 5                                  # 看得到 loss 走势
        --report_to none                                   # 不往 wandb 记冒烟 run
        --overwrite_output_dir
    )
    MODE="冒烟验证（20 步，不落 checkpoint，不报 wandb）"
fi

echo "=========================================================="
echo "  训练 group_${GROUP}"
echo "  模式        : ${MODE}"
echo "  配置        : ${CONFIG}"
echo "  可见 GPU    : ${CUDA_VISIBLE_DEVICES}（单卡，有效 batch = 1×16 = 16）"
echo "  NCCL P2P/IB : 已禁用（RTX 40 系不支持）"
echo "  启动方式    : torchrun（DeepSpeed 要求），nproc_per_node 应为 1"
echo "  启动时间    : $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================================="

exec uv run llamafactory-cli train "$CONFIG" "${EXTRA[@]}"
