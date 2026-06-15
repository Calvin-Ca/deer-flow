"""知识库构建编排 —— 阶段 1→3（承旧根 ``build.py``，改走 IR + 四层 factory）。

  structure  入 *_content_list.json（阶段 0 缓存）→ parser 解析成 Document → splitter 切分成 Chunk 树
             → 出 chunks.json（单一真值）+ structure.json（调试块）。
  reprs      入 Chunk 树 → feature.enrich 挂表征（免费 4 项）→ 重写 chunks.json（带表征）。
  index      入 带表征 Chunk 树 → index.view 选粒度 → index.build_index 建索引 → 出 BM25 + Milvus 双索引。

各层经 factory 选策略（parser_strategy / structure_strategy / features / index_granularity 均来自
profile），换解析模型/切法/表征/粒度只改 profile，不改本编排。从 ce-code 根运行：``python build.py …``
（根 build.py 是本模块 main 的薄壳）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console

import feature
from parser import factory as parser_factory
from splitter import factory as splitter_factory
from config import collection_name as build_collection_name
from core.profile import ParseProfile
from index import build_index, view

console = Console()

ROOT = Path(__file__).resolve().parent.parent  # ce-code/
DEFAULT_STRUCTURED = ROOT / "data" / "structured"
DEFAULT_VECTOR_STORE = ROOT / "data" / "vector_store"


def _safe(standard_id: str) -> str:
    """规范标识 → 安全目录名（structured / vector_store 统一用此）。"""
    return re.sub(r"[^\w]", "_", standard_id).strip("_")


def _write_json(data: list, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 写入 {path}（{len(data)} 条）[/green]")


def run_build(
    input_path: Path, *, standard_id: str = "", profile: ParseProfile | None = None,
    structured_dir: Path = DEFAULT_STRUCTURED, store_dir: Path | None = None,
    milvus_host: str = "localhost", milvus_port: int = 19530,
    embed_url: str = "http://localhost:8097", embed_model_id: str = "/model",
    batch_size: int = 64, bm25_only: bool = False,
) -> None:
    """阶段 1→3 编排（库函数；CLI 见 main）。preview 不在此（CLI 专用）。"""
    profile = profile or ParseProfile()
    base = input_path.stem.replace("_content_list", "")
    standard_id = standard_id or base

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]Profile：[/bold]{profile.name}  terminal_stage={profile.terminal_stage}  "
                  f"粒度={profile.index_granularity}")

    # provenance.source_file：尽量记为相对 data/parsed 的路径
    try:
        source_file = str(input_path.resolve().relative_to(ROOT / "data" / "parsed"))
    except ValueError:
        source_file = input_path.name

    # ── 阶段 0→1：解析成 Document ──
    with open(input_path, encoding="utf-8") as f:
        items = json.load(f)
    document = parser_factory.select(profile.parser_strategy).adapt(
        items, standard_id=standard_id, source_file=source_file)
    console.print(f"共 {len(document.blocks)} 个原始元素（{profile.parser_strategy} 解析）")

    # ── 阶段 1：切分建树 ──
    structured_out = structured_dir / _safe(standard_id) / profile.name
    structured_out.mkdir(parents=True, exist_ok=True)
    spl = splitter_factory.select(profile.structure_strategy)
    console.print(f"[bold cyan]切分[/bold cyan]（strategy={profile.structure_strategy}）→ "
                  f"{structured_out / 'chunks.json'}")
    result = spl.split(document, max_depth=profile.toc_max_depth,
                       subsplit=profile.subsplit)
    chunks = result.chunks
    if result.debug_blocks is not None:
        _write_json(result.debug_blocks, structured_out / "structure.json")
    _write_json([c.to_dict() for c in chunks], structured_out / "chunks.json")
    if profile.terminal_stage == "structure":
        console.print("[bold green]✓ 结构层完成（terminal_stage=structure）[/bold green]")
        return

    # ── 阶段 2：挂表征（全树；免费 4 项，向量待索引期算）──
    feature.enrich(chunks, list(profile.features))
    _write_json([c.to_dict() for c in chunks], structured_out / "chunks.json")  # 重写为带表征
    console.print(f"[cyan]表征[/cyan] 已挂 {profile.features} 到 {len(chunks)} 个节点")
    if profile.terminal_stage == "reprs":
        console.print("[bold green]✓ 表征层完成（terminal_stage=reprs）[/bold green]")
        return

    # ── 阶段 3：选粒度 + 建索引 ──
    if store_dir is None:
        store_dir = DEFAULT_VECTOR_STORE / _safe(standard_id) / profile.name
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    collection = build_collection_name(store_dir.name)

    units = view(chunks, profile.index_granularity)
    console.print(f"粒度 {profile.index_granularity} → {len(units)} 个检索单元 | collection={collection}")
    build_index(units, store_dir, collection, profile.index_granularity,
                milvus_host=milvus_host, milvus_port=milvus_port,
                embed_url=embed_url, embed_model_id=embed_model_id,
                batch_size=batch_size, bm25_only=bm25_only)
    console.print(f"[bold green]✓ 索引完成[/bold green] → {store_dir}（collection {collection}）")


@click.command()
@click.option("--input", "input_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="MinerU 输出的 *_content_list.json（阶段 0 缓存）路径。")
@click.option("--standard-id", default="", help="规范标识；不传则取输入文件名 basename。")
@click.option("--profile-name", default="default", show_default=True, help="parse_profile 名（产物子目录）。")
@click.option("--terminal-stage", type=click.Choice(["structure", "reprs", "index"]),
              default="index", show_default=True, help="流水线终止阶段。")
@click.option("--parser-strategy", default="mineru", show_default=True,
              help="解析模型（parser/ factory 键；缺省 mineru）。")
@click.option("--structure-strategy", default="toc", show_default=True,
              help="切分策略（splitter/ factory 键；缺省 toc=原生目录多层级）。")
@click.option("--toc-max-depth", type=int, default=None,
              help="切到第几级目录（号段层级，1=章/2=节/3=条…）；不传=全目录深度。")
@click.option("--subsplit", type=click.Choice(["none", "number"]),
              default="none", show_default=True,
              help="目录层下按编号细分：none=镜像目录不细分 / number=按编号号段再切出更细的编号子节点。")
@click.option("--index-granularity", type=click.Choice(["section", "clause", "paragraph"]),
              default="clause", show_default=True, help="索引粒度视图（view 选哪层 emit；当前仅 clause）。")
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
    parser_strategy: str, structure_strategy: str, toc_max_depth: int | None,
    subsplit: str, index_granularity: str,
    structured_dir: Path, store_dir: Path | None, milvus_host: str, milvus_port: int,
    embed_url: str, embed_model_id: str, batch_size: int, bm25_only: bool, preview: bool,
) -> None:
    """知识库构建（阶段 1→3）：解析 → 切分建树 →（表征）→（索引）。"""
    profile = ParseProfile(
        name=profile_name, terminal_stage=terminal_stage,
        parser_strategy=parser_strategy, structure_strategy=structure_strategy,
        toc_max_depth=toc_max_depth, subsplit=subsplit,
        index_granularity=index_granularity,
    )

    if preview:
        base = input_path.stem.replace("_content_list", "")
        sid = standard_id or base
        with open(input_path, encoding="utf-8") as f:
            items = json.load(f)
        document = parser_factory.select(profile.parser_strategy).adapt(
            items, standard_id=sid, source_file=input_path.name)
        result = splitter_factory.select(profile.structure_strategy).split(
            document, max_depth=profile.toc_max_depth, subsplit=profile.subsplit)
        chunks = result.chunks
        n_blk = len(result.debug_blocks) if result.debug_blocks is not None else 0
        console.print(f"[green]✓ 切分（{profile.structure_strategy}）：{n_blk} 块 → {len(chunks)} 个节点[/green]")
        console.print("\n[bold]--- 前 20 条节点预览 ---[/bold]")
        for c in chunks[:20]:
            tables = f"  [{len(c.tables)}表]" if c.tables else ""
            pg = c.provenance.page[0] if c.provenance.page else 0
            console.print(f"  [cyan]{c.node_path}[/cyan] [{c.chunk_type}] (p{pg}) {c.title[:50]}{tables}")
        return

    run_build(
        input_path, standard_id=standard_id, profile=profile,
        structured_dir=structured_dir, store_dir=store_dir,
        milvus_host=milvus_host, milvus_port=milvus_port,
        embed_url=embed_url, embed_model_id=embed_model_id,
        batch_size=batch_size, bm25_only=bm25_only,
    )


if __name__ == "__main__":
    main()
