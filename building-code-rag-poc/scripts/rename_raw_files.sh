#!/usr/bin/env bash
# 把 data/raw/ 下文件名中的空格替换为下划线。
# 用法：bash scripts/rename_raw_files.sh [--dry-run]
#   --dry-run  只打印，不实际重命名

set -euo pipefail

cd "$(dirname "$0")/.."   # 进入 building-code-rag-poc 根目录

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

changed=0
skipped=0

while IFS= read -r -d '' file; do
    dir=$(dirname "$file")
    base=$(basename "$file")
    newbase="${base// /_}"

    if [[ "$base" == "$newbase" ]]; then
        (( skipped++ )) || true
        continue
    fi

    newfile="$dir/$newbase"
    if $DRY_RUN; then
        echo "[dry-run] $base  →  $newbase"
    else
        mv -- "$file" "$newfile"
        echo "renamed: $base  →  $newbase"
    fi
    (( changed++ )) || true
done < <(find data/raw -maxdepth 1 -type f -print0 | sort -z)

echo
if $DRY_RUN; then
    echo "dry-run 完成：$changed 个文件需要重命名，$skipped 个无需处理。"
    echo "确认无误后去掉 --dry-run 参数再执行一次。"
else
    echo "完成：$changed 个文件已重命名，$skipped 个无需处理。"
fi
