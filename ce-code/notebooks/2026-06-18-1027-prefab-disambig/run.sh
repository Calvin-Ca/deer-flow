#!/usr/bin/env bash
# 实验 2026-06-18-1027 · 现浇/预制消歧。服务器（ce-code 根）执行。
# 用法：bash notebooks/2026-06-18-1027-prefab-disambig/run.sh 2>&1 | tee notebooks/2026-06-18-1027-prefab-disambig/results/run.log
set -euo pipefail

# ① 重建 2013 collection（带新 cast_type 字段；纯评测数据走 --from-jsonl 绕 PG）
uv run python -m cost.bill_index --from-jsonl data/structured/GB-50500-2013/bill_spec.jsonl --doc-id GB-50500-2013 --collection cost_bill_spec_kb_2013

# ② 专业过滤 + cast_type down-rank 重测
uv run python -m tools.eval_bill --gold data/eval_set/match_gold_2013.jsonl --collection cost_bill_spec_kb_2013 --code-prefix 01,03
