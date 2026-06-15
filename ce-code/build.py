"""build —— 知识库构建入口（本地命令行，一条命令跑完整流水线）。

入 *_content_list.json（阶段 0 缓存），单趟内存里跑完：

  ① 解析+切分  parser 解析成 Document → splitter 切分成 Chunk 树
               → 出 chunks.json（单一真值）+ catalog_blocks.json（每块目录标签·调试）。
  ② 选粒度     index.view 在 Chunk 树上选检索单元（clause = 已接地的无子节点·叶）。
  ③ 表征+索引  feature.build_features 对检索单元现算表征 sidecar（不挂 Chunk、不落 chunks.json）
               → index.build_index 建 BM25 + Milvus 双索引。

只想跑到前面某步不必动本编排：阶段 0 解析用 ``python -m parser``、只切分建树（本地无需
Milvus）用 ``python -m splitter`` 或 ``--preview``。各层经 factory 选策略（parser_strategy /
structure_strategy / features / index_granularity 均来自 profile），换解析模型/切法/表征/粒度
只改 profile，不改本编排。从 ce-code 根运行：

  python build.py --input data/parsed/<std>/auto/<std>_content_list.json             # 全量建库
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --preview   # 只测切分不落盘
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --bm25-only # 无 Milvus 只建 BM25
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console

from config import collection_name as build_collection_name
from core.profile import ParseProfile
from feature import build_features
from index import build_index, view
from parser import factory as parser_factory
from splitter import factory as splitter_factory

console = Console()

ROOT = Path(__file__).resolve().parent  # ce-code/
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
    """一趟跑完整流水线：解析 → 切分 → 选粒度 → 表征 → 索引（库函数；CLI 见 main）。preview 不在此。

    standard_id 缺省取输入文件名 basename；产物按 profile 隔离落
    ``structured/<std>/<profile>/`` 与 ``vector_store/<std>/<profile>/``。无 Milvus 时
    ``bm25_only=True`` 只建 BM25 + metadata。表征不落 chunks.json（索引期现算的 sidecar）。
    """
    profile = profile or ParseProfile()
    base = input_path.stem.replace("_content_list", "")
    standard_id = standard_id or base

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]Profile：[/bold]{profile.name}  粒度={profile.index_granularity}")

    # provenance.source_file：尽量记为相对 data/parsed 的路径
    try:
        source_file = str(input_path.resolve().relative_to(ROOT / "data" / "parsed"))
    except ValueError:
        source_file = input_path.name

    # ── ① 解析成 Document ──
    with open(input_path, encoding="utf-8") as f:
        items = json.load(f)
    document = parser_factory.select(profile.parser_strategy).adapt(
        items, standard_id=standard_id, source_file=source_file)
    console.print(f"共 {len(document.blocks)} 个原始元素（{profile.parser_strategy} 解析）")

    # ── ① 切分建树 → chunks.json（终态，无表征）+ catalog_blocks.json ──
    structured_out = structured_dir / _safe(standard_id) / profile.name
    structured_out.mkdir(parents=True, exist_ok=True)
    spl = splitter_factory.select(profile.structure_strategy)
    console.print(f"[bold cyan]切分[/bold cyan]（strategy={profile.structure_strategy}）→ "
                  f"{structured_out / 'chunks.json'}")
    result = spl.split(document, max_depth=profile.toc_max_depth,
                       subsplit=profile.subsplit)
    chunks = result.chunks
    if result.debug_blocks is not None:
        _write_json(result.debug_blocks, structured_out / "catalog_blocks.json")
    _write_json([c.to_dict() for c in chunks], structured_out / "chunks.json")

    # ── ② 选粒度 ──
    if store_dir is None:
        store_dir = DEFAULT_VECTOR_STORE / _safe(standard_id) / profile.name
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    collection = build_collection_name(store_dir.name)

    units = view(chunks, profile.index_granularity)
    console.print(f"粒度 {profile.index_granularity} → {len(units)} 个检索单元 | collection={collection}")

    # ── ③ 表征 sidecar（对检索单元现算，不挂 Chunk / 不落 chunks.json）+ 建索引 ──
    features = build_features(units, list(profile.features))
    console.print(f"[cyan]表征[/cyan] 已对 {len(units)} 个检索单元算 {profile.features}（sidecar，不落盘）")
    build_index(units, store_dir, collection, profile.index_granularity, features,
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
    input_path: Path, standard_id: str, profile_name: str,
    parser_strategy: str, structure_strategy: str, toc_max_depth: int | None,
    subsplit: str, index_granularity: str,
    structured_dir: Path, store_dir: Path | None, milvus_host: str, milvus_port: int,
    embed_url: str, embed_model_id: str, batch_size: int, bm25_only: bool, preview: bool,
) -> None:
    """知识库构建：一条命令跑完 解析 → 切分建树 → 选粒度 → 表征 → 索引。"""
    profile = ParseProfile(
        name=profile_name,
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
            kind = "container" if c.children_ids else "leaf"
            console.print(f"  [cyan]{c.node_path}[/cyan] [{kind}] (p{pg}) {c.title[:50]}{tables}")
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
