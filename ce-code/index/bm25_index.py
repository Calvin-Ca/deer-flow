"""BM25 索引 —— 消费 sparse 表征，建 rank-bm25 倒排（落 store_dir/bm25.pkl）。

承旧 ``retrieval/indexer.build_bm25``。语料 = 每个单元的 ``sparse`` 表征文本（node_path + title +
content 词项拼接），用 ``utils.tokenizer.tokenize`` 字符级分词（中文无空格；**与检索期同一套分词**，
杜绝召回错位）。落盘 ``{bm25, node_paths}``：node_paths 与语料同序，供检索期按 BM25 排名取行。
"""
from __future__ import annotations

import pickle
from pathlib import Path

from rich.console import Console

from core.chunk import Chunk
from core.feature import FeatureSidecar, feature_text
from utils.tokenizer import tokenize

console = Console()


def build(units: list[Chunk], store_dir: Path, features: FeatureSidecar) -> None:
    """建 BM25 索引并落 store_dir/bm25.pkl。

    参数：
        units (list[Chunk]): 检索单元。
        store_dir (Path): 输出目录。
        features (FeatureSidecar): 表征 sidecar（node_path → {kind: ChunkFeature}），取 sparse 文本。
    返回：
        无。
    """
    from rank_bm25 import BM25Okapi

    console.print("构建 BM25 索引…")
    corpus = [tokenize(feature_text(features.get(u.node_path, {}), "sparse")) for u in units]
    bm25 = BM25Okapi(corpus)

    out = store_dir / "bm25.pkl"
    with open(out, "wb") as f:
        pickle.dump({"bm25": bm25, "node_paths": [u.node_path for u in units]}, f)
    console.print(f"[green]✓ BM25 索引已写入 {out}[/green]")


def load(store_dir: Path):
    """读 store_dir/bm25.pkl，返回 (bm25, node_paths)（检索期 bm25_retriever 用）。"""
    with open(store_dir / "bm25.pkl", "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["node_paths"]
