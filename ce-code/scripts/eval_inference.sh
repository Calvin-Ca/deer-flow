#!/usr/bin/env bash
# 阶段 5.1：运行六模型推理或检查已有结果。
#
# 用法：
#   bash scripts/eval_inference.sh smoke smoke_20260730
#   bash scripts/eval_inference.sh full  eval_v1_20260730
#   bash scripts/eval_inference.sh check eval_v1_20260730
#
# 环境变量：
#   EVAL_BASE_URL=http://127.0.0.1:8002/v1
#   WORKERS=4
#   MODELS=base,base_fewshot,group_a,group_b,group_c,group_d
#   FEWSHOT_FILE=configs/prompts/eval_fewshot.json
#   MODEL_PATH=/mnt/nvme/calvin/models/Qwen2.5-7B-Instruct
#   CHECKPOINT_ROOT=checkpoints

set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-}"
RUN_ID="${2:-}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
EVAL_BASE_URL="${EVAL_BASE_URL:-http://127.0.0.1:8002/v1}"
WORKERS="${WORKERS:-4}"
MODELS="${MODELS:-base,base_fewshot,group_a,group_b,group_c,group_d}"
FEWSHOT_FILE="${FEWSHOT_FILE:-configs/prompts/eval_fewshot.json}"
MODEL_PATH="${MODEL_PATH:-/mnt/nvme/calvin/models/Qwen2.5-7B-Instruct}"
CHECKPOINT_ROOT="${CHECKPOINT_ROOT:-checkpoints}"

if [[ "$MODE" != "smoke" && "$MODE" != "full" && "$MODE" != "check" ]]; then
  echo "用法：bash scripts/eval_inference.sh {smoke|full|check} RUN_ID"
  exit 2
fi
if [[ -z "$RUN_ID" ]]; then
  echo "❌ 缺少 RUN_ID，例如 eval_v1_20260730"
  exit 2
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "❌ Python 不存在：$PYTHON_BIN"
  exit 1
fi
if [[ "$MODELS" == *"base_fewshot"* && ! -f "$FEWSHOT_FILE" ]]; then
  echo "❌ 六模型评测需要冻结的 3-shot 文件：$FEWSHOT_FILE"
  echo "   格式见 configs/prompts/eval_fewshot.example.json。"
  exit 1
fi

args=(
  -m src.eval.run_inference
  --run-id "$RUN_ID"
  --base-url "$EVAL_BASE_URL"
  --models "$MODELS"
  --fewshot-file "$FEWSHOT_FILE"
  --model-path "$MODEL_PATH"
  --checkpoint-root "$CHECKPOINT_ROOT"
  --workers "$WORKERS"
)

if [[ "$MODE" == "smoke" ]]; then
  args+=(--smoke)
elif [[ "$MODE" == "check" ]]; then
  args+=(--check-only)
fi

echo "=========================================================="
echo "  阶段 5.1 批量推理"
echo "  模式       : $MODE"
echo "  run_id     : $RUN_ID"
echo "  endpoint   : $EVAL_BASE_URL"
echo "  models     : $MODELS"
echo "  workers    : $WORKERS"
echo "  few-shot   : $FEWSHOT_FILE"
echo "=========================================================="

"$PYTHON_BIN" "${args[@]}"
