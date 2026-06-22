"""元数据快照 —— 把检索行落 store_dir/metadata.json（供不依赖 Milvus 的脚本/BM25 路径）。

承旧 ``retrieval/indexer.save_metadata``。每行即 ``index.manager.chunk_to_row`` 的标量字段，但
``references_to`` 在快照里保持 **list**（方便检索期引用扩展直接读，无需 json.loads）。BM25 检索路径
与 ``/clause`` 直取都从此文件取行（与 Milvus 行字段一致，零分歧）。
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

console = Console()


def save(rows: list[dict], store_dir: Path) -> None:
    """把行快照落 metadata.json（rows 的 references_to 应已为 list）。

    参数：
        rows (list[dict]): 检索行（references_to 为 list）。
        store_dir (Path): 输出目录。
    返回：
        无。
    """
    out = store_dir / "metadata.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 元数据快照已写入 {out}[/green]")


def load(store_dir: Path) -> list[dict]:
    """读 store_dir/metadata.json（检索期 BM25 路径 / 引用扩展 / clause 直取共用）。"""
    with open(store_dir / "metadata.json", encoding="utf-8") as f:
        return json.load(f)


def get_clause(store_dir: Path, node_path: str) -> dict | None:
    """按 node_path 从 metadata 直取单条（供 /clause 端点）。"""
    for m in load(store_dir):
        if m.get("node_path") == node_path:
            return dict(m)
    return None
