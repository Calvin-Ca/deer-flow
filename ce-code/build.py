#!/usr/bin/env python3
"""build —— 知识库构建编排（阶段 1→3，从 ce-code 根运行）。

合并旧 ``02_parse_hierarchy.py``（结构层）与 ``04_build_index.py``（索引层）为单一入口，
按 ``--terminal-stage`` 决定跑多远（语义即 parse_profile.terminal_stage）：

  structure  ① 格式适配（parser.FormatAdapter）→ ② 选 splitter 切分（splitter.get）
             → 写 nodes.json（单一真值）+ structure.json（调试）。
  reprs      structure + ③ reprs.enrich 给全树挂表征（免费 4 项）→ 重写 nodes.json。
  index      reprs + ④ view 选粒度 → indexer 建 BM25 + Milvus 双索引（按 profile 隔离）。

阶段 0（MinerU 解析）最贵、只跑一次、产物缓存于 data/parsed/，由 ``python -m parser`` 单独跑；
本入口从其缓存 *_content_list.json 起读。换 splitter / 粒度 / 表征只重跑本入口（不重跑
MinerU），切分纯 python 无模型、开销可忽略。

用法（从 ce-code 根）：
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --terminal-stage index
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console

from core.parse_profile import ParseProfile
from core.view import view
from parser import FormatAdapter
import reprs
import splitter
from retrieval.config import collection_name as build_collection_name

console = Console()

ROOT = Path(__file__).resolve().parent  # ce-code/
DEFAULT_STRUCTURED = ROOT / "data" / "structured"
DEFAULT_VECTOR_STORE = ROOT / "data" / "vector_store"


def _safe(standard_id: str) -> str:
    """规范标识 → 安全目录名（structured / vector_store 统一用此，消除旧 02/04 命名分歧）。"""
    return re.sub(r"[^\w]", "_", standard_id).strip("_")


# ---------------------------------------------------------------------------
# 阶段 1：切分建树
# ---------------------------------------------------------------------------

def run_structure(
    elements: list[dict], standard_id: str, profile: ParseProfile, out_dir: Path,
    *, source_file: str, version: str, effective_date: str, status: str,
) -> list[dict]:
    """选 splitter 切分 → 写 structure.json + nodes.json，返回节点树。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    spl = splitter.get(profile.structure_strategy)
    console.print(f"[bold cyan]切分[/bold cyan]（strategy={profile.structure_strategy}）→ {out_dir / 'nodes.json'}")
    result = spl.split(
        elements, standard_id=standard_id, profile=profile,
        source_file=source_file, version=version, effective_date=effective_date, status=status,
    )
    if result.debug_blocks is not None:
        _write_json(result.debug_blocks, out_dir / "structure.json")
    _write_json(result.nodes, out_dir / "nodes.json")
    return result.nodes


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def _write_json(data: list, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 写入 {path}（{len(data)} 条）[/green]")


@click.command()
@click.option("--input", "input_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="MinerU 输出的 *_content_list.json（v1，阶段 0 缓存）路径。")
@click.option("--standard-id", default="", help="规范标识；不传则取输入文件名 basename。")
@click.option("--profile-name", default="default", show_default=True, help="parse_profile 名（产物子目录）。")
@click.option("--terminal-stage", type=click.Choice(["structure", "reprs", "index"]),
              default="index", show_default=True, help="流水线终止阶段。")
@click.option("--structure-strategy", default="toc", show_default=True,
              help="切分策略（splitter/ REGISTRY 键；缺省 toc=原生目录多层级）。")
@click.option("--index-granularity", type=click.Choice(["section", "clause", "paragraph"]),
              default="clause", show_default=True, help="索引粒度视图（view 选哪层 emit；当前仅 clause）。")
@click.option("--version", default="", help="规范版本，如 2018。")
@click.option("--effective-date", default="", help="生效日期，如 2018-10-01。")
@click.option("--status", default="active", help="active / superseded / abolished。")
@click.option("--structured-dir", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_STRUCTURED, show_default=True, help="结构层产物根目录。")
@click.option("--store-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="索引存储目录，默认 data/vector_store/<standard>/<profile>/。")
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--embed-url", default="http://localhost:8097", show_default=True)
@click.option("--embed-model-id", default="/model", show_default=True)
@click.option("--batch-size", default=64, show_default=True, help="嵌入批大小。")
@click.option("--bm25-only", is_flag=True, help="索引阶段只建 BM25，跳过向量索引。")
@click.option("--preview", is_flag=True, help="只打印前 20 条节点，不写文件。")
def main(
    input_path: Path, standard_id: str, profile_name: str, terminal_stage: str,
    structure_strategy: str, index_granularity: str, version: str, effective_date: str,
    status: str, structured_dir: Path, store_dir: Path | None, milvus_host: str,
    milvus_port: int, embed_url: str, embed_model_id: str, batch_size: int,
    bm25_only: bool, preview: bool,
) -> None:
    """知识库构建（阶段 1→3）：格式适配 → 切分建树 →（reprs）→（索引）。"""
    base = input_path.stem.replace("_content_list", "")
    if not standard_id:
        standard_id = base
    profile = ParseProfile(
        name=profile_name, terminal_stage=terminal_stage,
        structure_strategy=structure_strategy, index_granularity=index_granularity,
    )

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]Profile：[/bold]{profile.name}  terminal_stage={profile.terminal_stage}  粒度={index_granularity}")

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)
    elements = FormatAdapter.adapt(data)
    console.print(f"共 {len(elements)} 个原始元素（page_number 已过滤）")

    # preview：只切分、打印前 20 节点，不写盘
    if preview:
        result = splitter.get(profile.structure_strategy).split(
            elements, standard_id=standard_id, profile=profile)
        chunks = result.nodes
        n_blk = len(result.debug_blocks) if result.debug_blocks is not None else 0
        console.print(f"[green]✓ 切分（{profile.structure_strategy}）：{n_blk} 块 → {len(chunks)} 个节点[/green]")
        console.print("\n[bold]--- 前 20 条节点预览 ---[/bold]")
        for c in chunks[:20]:
            tables = f"  [{len(c['tables'])}表]" if c.get("tables") else ""
            console.print(f"  [cyan]{c['clause_path']}[/cyan] [{c.get('node_type','?')}] (p{c['page']}) {c['title'][:50]}{tables}")
        return

    # provenance.source_file：尽量记为相对 data/parsed 的路径，回指阶段 0 缓存
    try:
        source_file = str(input_path.resolve().relative_to(ROOT / "data" / "parsed"))
    except ValueError:
        source_file = input_path.name

    # ── 阶段 1：切分建树 ──
    structured_out = structured_dir / _safe(standard_id) / profile.name
    nodes = run_structure(
        elements, standard_id, profile, structured_out,
        source_file=source_file, version=version, effective_date=effective_date, status=status,
    )
    if profile.terminal_stage == "structure":
        console.print("[bold green]✓ 结构层完成（terminal_stage=structure）[/bold green]")
        return

    # ── 阶段 2：挂表征（全树；免费 4 项，向量待索引期算）──
    reprs.enrich(nodes, list(profile.reprs))
    _write_json(nodes, structured_out / "nodes.json")  # 重写为带 reprs 的节点库
    console.print(f"[cyan]表征[/cyan] 已挂 {profile.reprs} 到 {len(nodes)} 个节点")
    if profile.terminal_stage == "reprs":
        console.print("[bold green]✓ 表征层完成（terminal_stage=reprs）[/bold green]")
        return

    # ── 阶段 3：选粒度 + 建索引 ──
    from retrieval import indexer  # lazy：索引依赖（pymilvus/rank_bm25）按需加载

    if store_dir is None:
        store_dir = DEFAULT_VECTOR_STORE / _safe(standard_id) / profile.name
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    collection = build_collection_name(store_dir.name)  # 由 profile 名推断，与 server/eval 一致

    units = view(nodes, profile.index_granularity)
    console.print(f"粒度 {profile.index_granularity} → {len(units)} 个检索单元 | collection={collection}")
    indexer.build_index(
        units, store_dir, collection, profile.index_granularity,
        milvus_host=milvus_host, milvus_port=milvus_port,
        embed_url=embed_url, embed_model_id=embed_model_id,
        batch_size=batch_size, bm25_only=bm25_only,
    )
    console.print(f"[bold green]✓ 索引完成[/bold green] → {store_dir}（collection {collection}）")


if __name__ == "__main__":
    main()
