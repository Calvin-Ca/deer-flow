#!/usr/bin/env bash
# 阶段 5.1：单次加载 Qwen2.5-7B-Instruct，并注册四个 LoRA adapter。
#
# 默认使用物理 GPU 0，服务地址 http://127.0.0.1:8002/v1。
# 可覆盖示例：
#   CUDA_VISIBLE_DEVICES=2 PORT=8002 bash scripts/serve_eval.sh
# vLLM 0.6.x 尚不支持新版的 `--generation-config vllm` 特殊值：
#   GENERATION_CONFIG=none VLLM_BIN=/path/to/vllm bash scripts/serve_eval.sh
#
# 本脚本占用当前终端持续运行；请另开一个终端执行 eval_inference.sh。

set -euo pipefail

cd "$(dirname "$0")/.."

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
MODEL_PATH="${MODEL_PATH:-/mnt/nvme/calvin/models/Qwen2.5-7B-Instruct}"
CHECKPOINT_ROOT="${CHECKPOINT_ROOT:-checkpoints}"
PORT="${PORT:-8002}"
HOST="${HOST:-127.0.0.1}"
VLLM_BIN="${VLLM_BIN:-.venv/bin/vllm}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GENERATION_CONFIG="${GENERATION_CONFIG:-vllm}"

generation_config_args=()
if [[ "$GENERATION_CONFIG" != "none" ]]; then
  generation_config_args=(--generation-config "$GENERATION_CONFIG")
fi

if [[ ! -x "$VLLM_BIN" ]]; then
  echo "❌ 找不到 vLLM 命令：$VLLM_BIN"
  echo "   请确认使用服务器项目环境，或通过 VLLM_BIN=/实际路径/vllm 指定。"
  exit 1
fi

if [[ ! -d "$MODEL_PATH" ]]; then
  echo "❌ 基座模型目录不存在：$MODEL_PATH"
  exit 1
fi

for group in a b c d; do
  adapter="$CHECKPOINT_ROOT/group_${group}"
  if [[ ! -f "$adapter/adapter_model.safetensors" ]] ||
     [[ ! -f "$adapter/adapter_config.json" ]]; then
    echo "❌ adapter 不完整：$adapter"
    exit 1
  fi
done

echo "=========================================================="
echo "  vLLM 多 LoRA 评测服务"
echo "  GPU                 : $CUDA_VISIBLE_DEVICES"
echo "  基座                : $MODEL_PATH"
echo "  地址                : http://$HOST:$PORT/v1"
echo "  max_model_len       : $MAX_MODEL_LEN"
echo "  gpu_memory_util     : $GPU_MEMORY_UTILIZATION"
echo "  generation_config   : $GENERATION_CONFIG"
echo "  模型名              : base / group_a / group_b / group_c / group_d"
echo "=========================================================="

exec "$VLLM_BIN" serve "$MODEL_PATH" \
  --host "$HOST" \
  --port "$PORT" \
  --served-model-name base \
  --dtype bfloat16 \
  --seed 42 \
  "${generation_config_args[@]}" \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --enable-lora \
  --max-lora-rank 32 \
  --max-loras 1 \
  --max-cpu-loras 4 \
  --lora-modules \
    "group_a=$CHECKPOINT_ROOT/group_a" \
    "group_b=$CHECKPOINT_ROOT/group_b" \
    "group_c=$CHECKPOINT_ROOT/group_c" \
    "group_d=$CHECKPOINT_ROOT/group_d"
