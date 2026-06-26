#!/usr/bin/env bash
# 实验运行脚本（服务器执行）。约定：每条命令单行、无 \ 续行（见仓库根 CLAUDE.md）。
# 用法（服务器，ce-code 根）：bash notebooks/<本文件夹>/run.sh 2>&1 | tee notebooks/<本文件夹>/results/run.log
set -euo pipefail

# 示例（按本次实验替换）：
# uv run python -m cost.bill_index --from-jsonl data/structured/<doc>/bill_spec.jsonl --doc-id <doc> --collection <coll>
# uv run python -m tools.eval_bill --gold data/eval_set/<gold>.jsonl --collection <coll> --code-prefix 01,03
