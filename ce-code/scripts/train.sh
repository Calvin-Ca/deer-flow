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
#                           从 2×8=16 变成 2×8×4=64。
#   NCCL_P2P_DISABLE=1      RTX 40 系消费卡被 NVIDIA 关闭了 P2P，
#   NCCL_IB_DISABLE=1       不设则 accelerate 抛 NotImplementedError 直接启动失败。
#
# 用法：
#   ./scripts/train.sh a          # 训练 group_a
#   ./scripts/train.sh {a|b|c|d}
#
# 输入：组别字母（a/b/c/d），对应 configs/group_<x>.yaml
# 输出：checkpoints/group_<x>/（LoRA adapter + 训练日志），wandb run 名为 group_<x>
set -euo pipefail

GROUP="${1:-}"
if [[ ! "$GROUP" =~ ^[abcd]$ ]]; then
    echo "用法: $0 {a|b|c|d}" >&2
    exit 1
fi

CONFIG="configs/group_${GROUP}.yaml"
if [[ ! -f "$CONFIG" ]]; then
    echo "找不到配置文件: $CONFIG（请在 ce-code/ 目录下执行本脚本）" >&2
    exit 1
fi

# ── 四组必须完全一致的环境（改动须同步四组并记入 EXPERIMENT.md §6）──────────
export CUDA_VISIBLE_DEVICES=0
export NCCL_P2P_DISABLE=1
export NCCL_IB_DISABLE=1

echo "=========================================================="
echo "  训练 group_${GROUP}"
echo "  配置        : ${CONFIG}"
echo "  可见 GPU    : ${CUDA_VISIBLE_DEVICES}（单卡，有效 batch = 2×8 = 16）"
echo "  NCCL P2P/IB : 已禁用（RTX 40 系不支持）"
echo "  启动时间    : $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================================="

exec uv run llamafactory-cli train "$CONFIG"
