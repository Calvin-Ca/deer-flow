#!/usr/bin/env bash
# 实验 2026-06-18-1558 · 2013 换正确源（GB 50854-2013 房建计量规范）重抽 + 重测。
# 服务器（ce-code 根）执行。约定：每条命令单行、无 \ 续行。
# 用法：bash notebooks/2026-06-18-1558-2013-switch-gb50854/run.sh 2>&1 | tee notebooks/2026-06-18-1558-2013-switch-gb50854/results/run.log
set -euo pipefail

# ① 从正确源抽 2013 bill_spec（doc_id 归一为 GB-50854-2013，落 data/structured/GB-50854-2013/）
uv run python -m cost.bill_spec --input "data/structured/GB_50854_2013_房屋建筑与装饰工程工程量计算规范/default/chunks.json"

# ② 用新 bill_spec 重建 gold（覆盖过滤对齐 GB-50854-2013；安装类 03 码不在房建库 → 自动落 uncovered）
uv run python -m tools.build_match_gold --xlsx data/eval_set/清单量关联实物量数据.xlsx --bill-spec data/structured/GB-50854-2013/bill_spec.jsonl --out data/eval_set/match_gold_2013.jsonl

# ③ 重建 2013 collection（直读新 jsonl，绕 PG；带 cast_type 现浇/预制标记）
uv run python -m cost.bill_index --from-jsonl data/structured/GB-50854-2013/bill_spec.jsonl --doc-id GB-50854-2013 --collection cost_bill_spec_kb_2013

# ④ 重测（房建单专业库，无需 code-prefix；structural+cast_type 默认开）
uv run python -m tools.eval_bill --gold data/eval_set/match_gold_2013.jsonl --collection cost_bill_spec_kb_2013
