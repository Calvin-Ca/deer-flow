"""清理 Langfuse dataset 里的残留 item（归档 ARCHIVED，UI 不再列出、runner 不再跑）。

背景：``create_dataset_item`` 只覆盖同 id、不删除——金标删条/改 id 命名空间后，旧 item 会
残留在 dataset 里继续被 runner 迭代（实锤：主池 78 条跑出 127，含上次上传中途 404 前写入的
49 条无前缀残留）。本脚本按「保留 id 集合 = 当前金标文件」清理，不在集合内的一律归档。

运行（服务器）：
    uv run --project backend python benchmark/_shared/prune_dataset_items.py --dataset user-requests-routing --keep-prefix ur- --dry-run
    确认清单无误后去掉 --dry-run 真删。
    专项集同理：--dataset bill-match-routing --gold benchmark/L1_routing/data/bill_match_routing.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_GOLD_BY_DATASET = {
    "user-requests-routing": (_ROOT / "benchmark" / "L1_routing" / "data" / "user_requests.jsonl", "ur-"),
    "bill-match-routing": (_ROOT / "benchmark" / "L1_routing" / "data" / "bill_match_routing.jsonl", ""),
}


def _gold_ids(jsonl: Path, prefix: str) -> set[str]:
    ids = set()
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            ids.add(f"{prefix}{json.loads(line)['id']}")
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="归档 Langfuse dataset 里不在当前金标内的残留 item")
    parser.add_argument("--dataset", required=True, help="Langfuse dataset 名")
    parser.add_argument("--gold", default=None, help="金标 jsonl（缺省按 dataset 名查内置映射）")
    parser.add_argument("--keep-prefix", default=None, help="item id 前缀（缺省按内置映射）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将归档的 item，不动手")
    args = parser.parse_args()

    if args.gold:
        gold_path, prefix = Path(args.gold), (args.keep_prefix or "")
    elif args.dataset in _GOLD_BY_DATASET:
        gold_path, prefix = _GOLD_BY_DATASET[args.dataset]
        if args.keep_prefix is not None:
            prefix = args.keep_prefix
    else:
        raise SystemExit(f"未知 dataset {args.dataset!r}，请显式给 --gold")

    keep = _gold_ids(gold_path, prefix)
    client = require_langfuse()
    dataset = client.get_dataset(args.dataset)
    items = list(dataset.items)
    stale = [item for item in items if item.id not in keep]
    print(f"dataset «{args.dataset}»：现有 {len(items)} 条，金标应有 {len(keep)} 条，待归档 {len(stale)} 条")
    for item in stale:
        query = ""
        if isinstance(item.input, dict):
            query = str(item.input.get("query") or item.input.get("description") or "")[:40]
        print(f"  {'[dry-run] ' if args.dry_run else ''}归档 {item.id}  {query}")
        if not args.dry_run:
            # ARCHIVED 状态：UI 默认不列、dataset.items 不再返回（Langfuse 无硬删 API，归档即退役）
            client.api.dataset_items.create(
                id=item.id,
                dataset_name=args.dataset,
                input=item.input,
                expected_output=item.expected_output,
                metadata=item.metadata,
                status="ARCHIVED",
            )
    if not args.dry_run and stale:
        client.flush()
        print(f"✓ 已归档 {len(stale)} 条；建议重跑 runner 前用新 --run-name（本轮 run 已混入残留条目）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
