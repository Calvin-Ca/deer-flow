#!/usr/bin/env bash
# 实验 2026-06-18-1613 · 修 cast_type MinerU 空格 bug 后复跑（救柱本体桶）。
# 服务器（ce-code 根）执行。bill_spec/gold 不变，只重建 collection（cast_type 重新派生）+ 重测。
# 用法：bash notebooks/2026-06-18-1613-casttype-spacefix/run.sh 2>&1 | tee notebooks/2026-06-18-1613-casttype-spacefix/results/run.log
set -euo pipefail

# ① 重建 2013 collection（cast_type 去空格归一后，预制 caption "预 制" 能正确判为预制）
uv run python -m cost.bill_index --from-jsonl data/structured/GB-50854-2013/bill_spec.jsonl --doc-id GB-50854-2013 --collection cost_bill_spec_kb_2013

# ② 重测（gold 不变；对比 E8 看柱本体桶是否回正）
uv run python -m tools.eval_bill --gold data/eval_set/match_gold_2013.jsonl --collection cost_bill_spec_kb_2013
