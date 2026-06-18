#!/usr/bin/env bash
# 实验 2026-06-18-2143 · 模板索引文本增强（caption 子表类别注入嵌入）复跑。
# 服务器（ce-code 根）执行。bill_spec/gold 不变，只重建 collection（嵌入文本变了）+ 重测。
# 用法：bash notebooks/2026-06-18-2143-template-index-enrich/run.sh 2>&1 | tee notebooks/2026-06-18-2143-template-index-enrich/results/run.log
set -euo pipefail

# ① 重建 2013 collection（嵌入文本现含 caption 子表类别，措施码补回"模板/脚手架"等信号）
uv run python -m cost.bill_index --spec 2013

# ② 重测（gold 不变；对比 E9 看措施桶 0117 召回是否抬升）
uv run python -m tools.eval_bill --gold data/eval_set/match_gold_2013.jsonl --collection cost_bill_spec_kb_2013

# （可选）2024 库一并享受同款增强：uv run python -m cost.bill_index --spec 2024
