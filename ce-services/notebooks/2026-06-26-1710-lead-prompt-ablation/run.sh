#!/usr/bin/env bash
# lead-agent 提示词消融实验 · 服务器执行脚本
# 约定：每条命令独立单行，无 `\` 续行（见仓库根 CLAUDE.md）。
# 前置：config.yaml 指向 qwen3-8b；:8099 LLM / :8100 知识 / :8101 任务 起齐
#       （DeerFlowClient 为 in-process，无需 Gateway；但脚本真调 :8101 需任务服务在）。
# 跑法：git pull 后在仓库根执行 `bash ce-services/notebooks/2026-06-26-1710-lead-prompt-ablation/run.sh`
set -e
cd "$(git rev-parse --show-toplevel)"
EXP=ce-services/notebooks/2026-06-26-1710-lead-prompt-ablation
uv run --project backend python "$EXP/harness.py" --model qwen3-8b 2>&1 | tee "$EXP/results/run.log"
