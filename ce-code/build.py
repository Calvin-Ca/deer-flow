#!/usr/bin/env python3
"""build —— 知识库构建入口（解析 → 切分 → 选粒度 → 表征 → 索引；一次跑完 / 分步跑同源）。

本文件同时提供两种跑法，**共用同一批 ``step_*`` 步骤函数、落同一批中间产物**，故「一次跑完」与
「分步跑」产出逐字一致——想一把梭用 ``all``，想逐步审核中间结果用 ``parse/split/view/feature/index``：

  python build.py all     --input data/parsed/<std>/auto/<std>_content_list.json   # 一趟跑完整流水线
  python build.py all     --input ... --preview                                    # 只测切分不落盘
  python build.py all     --input ... --bm25-only                                  # 无 Milvus 只建 BM25
  python build.py parse   --input ...                                              # ① → document.json
  python build.py split   --standard-id <std> --subsplit number                    # ② → chunks.json
  python build.py view    --standard-id <std>                                       # ③ → units.json
  python build.py feature --standard-id <std>                                       # ④ → features.json
  python build.py index   --standard-id <std> [--bm25-only]                         # ⑤ → store（终态）

中间产物（结构层落 ``data/structured/<std>/<profile>/``，索引落 ``data/vector_store/<std>/<profile>/``）：

  ① 解析    ``document.json``       Document（格式归一后的纯版面块流，未打标）。
  ② 切分    ``chunks.json``         Chunk 树（单一真值）+ ``catalog_blocks.json``（目录打标快照·调试）。
  ③ 选粒度  ``units.json``          检索单元（view 在树上选的某层 emit；clause = 已接地的叶）。
  ④ 表征    ``features.json``       表征 sidecar（``node_path → {kind: ChunkFeature}``；dense 向量留索引期填）。
  ⑤ 索引    ``bm25.pkl`` / ``metadata.json`` / Milvus collection（终态）。

各层经 factory 按 profile 选策略（parser_strategy / structure_strategy / features /
index_granularity），换解析模型/切法/表征/粒度只改 profile。``standard_id`` 缺省取输入文件名
basename；产物按 ``{std}/{profile}`` 隔离，store 名与 collection 由 ``config`` 统一推断。
分步子命令靠 ``--standard-id`` + ``--profile-name`` 定位 structured 目录（与 splitter 同 ``_safe`` 规则）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console

from config import collection_name as _collection_name
from feature import build_features
from index import build_index, view
from ingest.ir.chunk import Chunk
from ingest.ir.document import Document
from ir.feature import ChunkFeature, FeatureSidecar
from ingest.ir.profile import ParseProfile
from ingest.parser import factory as parser_factory
from ingest.splitter import factory as splitter_factory
from ingest.splitter.toc_splitter import print_catalog_stats

console = Console()

ROOT = Path(__file__).resolve().parent  # ce-code/
DEFAULT_STRUCTURED = ROOT / "data" / "structured"
DEFAULT_VECTOR_STORE = ROOT / "data" / "vector_store"

# 各步中间产物文件名（一次跑完写 / 分步跑读，单一真值，勿散落字面量）
DOCUMENT_JSON = "document.json"
CHUNKS_JSON = "chunks.json"
CATALOG_BLOCKS_JSON = "catalog_blocks.json"
UNITS_JSON = "units.json"
FEATURES_JSON = "features.json"


# ── 路径解析（一次跑完 / 分步跑共用，保证落盘位置一致）──────────────────────────

def safe_std(standard_id: str) -> str:
    """规范标识 → 安全目录名（structured / vector_store 统一用此；与 splitter 逐字一致）。"""
    return re.sub(r"[^\w]", "_", standard_id).strip("_")


def standard_id_of(input_path: Path | None, standard_id: str = "") -> str:
    """定规范标识：显式 standard_id 优先，否则取输入 content_list 文件名 basename。"""
    if standard_id:
        return standard_id
    if input_path is not None:
        return input_path.stem.replace("_content_list", "")
    raise ValueError("无法确定 standard_id：请传 --standard-id 或 --input")


def structured_out_dir(standard_id: str, profile_name: str,
                       structured_dir: Path = DEFAULT_STRUCTURED) -> Path:
    """结构层产物目录 ``data/structured/<safe_std>/<profile>/``（中间产物落此）。"""
    return Path(structured_dir) / safe_std(standard_id) / profile_name


def store_out_dir(standard_id: str, profile_name: str,
                  store_root: Path = DEFAULT_VECTOR_STORE) -> Path:
    """索引存储目录 ``data/vector_store/<safe_std>/<profile>/``（终态索引落此）。"""
    return Path(store_root) / safe_std(standard_id) / profile_name


def collection_of(store_dir: Path) -> str:
    """store 目录名 → Milvus collection 名（与 service/eval 共享推断逻辑）。"""
    return _collection_name(Path(store_dir).name)


# ── 落盘 / 载入小工具 ──────────────────────────────────────────────────────────

def _dump(data, path: Path, n: int) -> None:
    """落 JSON（UTF-8、缩进、不转义中文）并打印条数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 写入 {path}（{n} 条）[/green]")


def _load(path: Path):
    """读 JSON（缺文件给出语义化错误，提示先跑上一步）。"""
    if not path.exists():
        raise FileNotFoundError(f"缺中间产物 {path}（请先跑产出它的上一步）")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── 步骤函数（每步落中间产物 + 返回内存对象；一次跑完串这五个，分步跑各调一个）──

def step_parse(input_path: Path, *, standard_id: str, profile: ParseProfile,
               structured_out: Path) -> Document:
    """① 解析：content_list.json → Document（格式归一），落 ``document.json``。

    参数：
        input_path (Path): MinerU 输出的 *_content_list.json（阶段 0 缓存）。
        standard_id (str): 规范标识（逐块继承到 Chunk）。
        profile (ParseProfile): 取 ``parser_strategy`` 选解析模型。
        structured_out (Path): 结构层产物目录。
    返回：
        Document: 有序 Block 列表（纯版面块）。
    """
    # provenance.source_file：尽量记为相对 data/parsed 的路径（溯源回阶段 0 缓存）
    try:
        source_file = str(input_path.resolve().relative_to(ROOT / "data" / "parsed"))
    except ValueError:
        source_file = input_path.name

    with open(input_path, encoding="utf-8") as f:
        items = json.load(f)
    document = parser_factory.select(profile.parser_strategy).adapt(
        items, standard_id=standard_id, source_file=source_file)
    console.print(f"[bold cyan]① 解析[/bold cyan]（{profile.parser_strategy}）→ "
                  f"{len(document.blocks)} 个原始元素")
    _dump(document.to_dict(), structured_out / DOCUMENT_JSON, len(document.blocks))
    return document


def step_split(document: Document, *, profile: ParseProfile,
               structured_out: Path) -> list[Chunk]:
    """② 切分：Document → Chunk 树，落 ``chunks.json``（+ ``catalog_blocks.json`` 调试快照）。

    参数：
        document (Document): 解析层产物。
        profile (ParseProfile): 取 ``structure_strategy`` / ``toc_max_depth`` / ``subsplit``。
        structured_out (Path): 结构层产物目录。
    返回：
        list[Chunk]: Chunk 树（单一真值）。
    """
    spl = splitter_factory.select(profile.structure_strategy)
    console.print(f"[bold cyan]② 切分[/bold cyan]（strategy={profile.structure_strategy} "
                  f"max_depth={profile.toc_max_depth} subsplit={profile.subsplit}）")
    result = spl.split(document, max_depth=profile.toc_max_depth, subsplit=profile.subsplit)
    if result.catalog_blocks:  # toc 法目录打标快照（其余切法留空 → 不落盘）
        print_catalog_stats(result.catalog_blocks)
        _dump(result.catalog_blocks, structured_out / CATALOG_BLOCKS_JSON,
              len(result.catalog_blocks))
    _dump([c.to_dict() for c in result.chunks], structured_out / CHUNKS_JSON,
          len(result.chunks))
    return result.chunks


def step_view(chunks: list[Chunk], *, profile: ParseProfile,
              structured_out: Path) -> list[Chunk]:
    """③ 选粒度：在 Chunk 树上选检索单元，落 ``units.json``。

    参数：
        chunks (list[Chunk]): 完整 Chunk 树。
        profile (ParseProfile): 取 ``index_granularity``（当前仅 clause）。
        structured_out (Path): 结构层产物目录。
    返回：
        list[Chunk]: 检索单元（已剔除未接地空骨架）。
    """
    units = view(chunks, profile.index_granularity)
    console.print(f"[bold cyan]③ 选粒度[/bold cyan]（{profile.index_granularity}）→ "
                  f"{len(units)} 个检索单元")
    _dump([u.to_dict() for u in units], structured_out / UNITS_JSON, len(units))
    return units


def step_feature(units: list[Chunk], *, profile: ParseProfile,
                 structured_out: Path) -> FeatureSidecar:
    """④ 表征：对检索单元算表征 sidecar，落 ``features.json``（dense 向量留索引期填）。

    参数：
        units (list[Chunk]): 检索单元。
        profile (ParseProfile): 取 ``features`` 启用集。
        structured_out (Path): 结构层产物目录。
    返回：
        FeatureSidecar: node_path → {kind: ChunkFeature}。
    """
    features = build_features(units, list(profile.features))
    console.print(f"[bold cyan]④ 表征[/bold cyan] 对 {len(units)} 个单元算 {profile.features}")
    serial = {path: {kind: f.to_dict() for kind, f in fmap.items()}
              for path, fmap in features.items()}
    _dump(serial, structured_out / FEATURES_JSON, len(serial))
    return features


def step_index(units: list[Chunk], features: FeatureSidecar, *, profile: ParseProfile,
               store_dir: Path, collection: str, milvus_host: str = "localhost",
               milvus_port: int = 19530, embed_url: str = "http://localhost:8097",
               embed_model_id: str = "/model", batch_size: int = 64,
               bm25_only: bool = False) -> None:
    """⑤ 索引：检索单元 + 表征 → BM25 + metadata（+ 可选 Milvus 向量），落 store_dir（终态）。

    参数：
        units (list[Chunk]): 检索单元。
        features (FeatureSidecar): 表征 sidecar。
        profile (ParseProfile): 取 ``index_granularity`` 写入每行。
        store_dir (Path): 索引存储目录。
        collection (str): Milvus collection 名。
        bm25_only (bool): 只建 BM25 + metadata，跳过向量索引（无 embedding/Milvus 时用）。
    返回：
        无（产物写 store_dir）。
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold cyan]⑤ 索引[/bold cyan] → {store_dir}（collection={collection}"
                  f"{'，bm25_only' if bm25_only else ''}）")
    build_index(units, store_dir, collection, profile.index_granularity, features,
                milvus_host=milvus_host, milvus_port=milvus_port,
                embed_url=embed_url, embed_model_id=embed_model_id,
                batch_size=batch_size, bm25_only=bm25_only)
    console.print(f"[bold green]✓ 索引完成[/bold green] → {store_dir}")


# ── 中间产物载入（分步跑从盘上前一步产物起跑用）──────────────────────────────

def load_document(structured_out: Path) -> Document:
    """读 ``document.json`` → Document。"""
    return Document.from_dict(_load(structured_out / DOCUMENT_JSON))


def load_chunks(structured_out: Path) -> list[Chunk]:
    """读 ``chunks.json`` → Chunk 树。"""
    return [Chunk.from_dict(d) for d in _load(structured_out / CHUNKS_JSON)]


def load_units(structured_out: Path) -> list[Chunk]:
    """读 ``units.json`` → 检索单元。"""
    return [Chunk.from_dict(d) for d in _load(structured_out / UNITS_JSON)]


def load_features(structured_out: Path) -> FeatureSidecar:
    """读 ``features.json`` → 表征 sidecar（还原 ChunkFeature）。"""
    raw = _load(structured_out / FEATURES_JSON)
    return {path: {kind: ChunkFeature.from_dict(fd) for kind, fd in fmap.items()}
            for path, fmap in raw.items()}


# ── 一次跑完（库函数）──────────────────────────────────────────────────────────

def run_build(
    input_path: Path, *, standard_id: str = "", profile: ParseProfile | None = None,
    structured_dir: Path = DEFAULT_STRUCTURED, store_dir: Path | None = None,
    milvus_host: str = "localhost", milvus_port: int = 19530,
    embed_url: str = "http://localhost:8097", embed_model_id: str = "/model",
    batch_size: int = 64, bm25_only: bool = False,
) -> None:
    """一趟跑完整流水线：解析 → 切分 → 选粒度 → 表征 → 索引（库函数；CLI 见 ``all``）。preview 不在此。

    内存里把五步串起来、**逐步落盘**（与分步子命令产出一致）。standard_id 缺省取输入文件名
    basename；产物按 profile 隔离落 ``structured/<std>/<profile>/`` 与 ``vector_store/<std>/<profile>/``。
    无 Milvus 时 ``bm25_only=True`` 只建 BM25 + metadata。
    """
    profile = profile or ParseProfile()
    standard_id = standard_id_of(input_path, standard_id)
    structured_out = structured_out_dir(standard_id, profile.name, structured_dir)
    store_dir = (Path(store_dir) if store_dir is not None
                 else store_out_dir(standard_id, profile.name))
    collection = collection_of(store_dir)

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]Profile：[/bold]{profile.name}  粒度={profile.index_granularity}"
                  f"  collection={collection}")

    document = step_parse(input_path, standard_id=standard_id, profile=profile,
                          structured_out=structured_out)
    chunks = step_split(document, profile=profile, structured_out=structured_out)
    units = step_view(chunks, profile=profile, structured_out=structured_out)
    features = step_feature(units, profile=profile, structured_out=structured_out)
    step_index(units, features, profile=profile, store_dir=store_dir, collection=collection,
               milvus_host=milvus_host, milvus_port=milvus_port, embed_url=embed_url,
               embed_model_id=embed_model_id, batch_size=batch_size, bm25_only=bm25_only)


# ── CLI（命令组：all 一次跑完 + parse/split/view/feature/index 分步）─────────────

@click.group()
def cli() -> None:
    """知识库构建：``all`` 一次跑完整流水线，或 ``parse/split/view/feature/index`` 逐步审核。"""


def _locate_opts(fn):
    """挂 ``--standard-id`` / ``--profile-name`` / ``--structured-dir`` 三个定位选项（分步子命令共用）。"""
    fn = click.option("--structured-dir", type=click.Path(file_okay=False, path_type=Path),
                      default=DEFAULT_STRUCTURED, show_default=True,
                      help="结构层产物根目录。")(fn)
    fn = click.option("--profile-name", default="default", show_default=True,
                      help="profile 名（产物子目录）。")(fn)
    fn = click.option("--standard-id", default="",
                      help="规范标识（定位 structured/<std>/ 目录）；parse 不传则取输入 basename。")(fn)
    return fn


@cli.command("all")
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
def cmd_all(
    input_path: Path, standard_id: str, profile_name: str,
    parser_strategy: str, structure_strategy: str, toc_max_depth: int | None,
    subsplit: str, index_granularity: str,
    structured_dir: Path, store_dir: Path | None, milvus_host: str, milvus_port: int,
    embed_url: str, embed_model_id: str, batch_size: int, bm25_only: bool, preview: bool,
) -> None:
    """一条命令跑完 解析 → 切分建树 → 选粒度 → 表征 → 索引（逐步落盘，与分步跑产出一致）。"""
    profile = ParseProfile(
        name=profile_name,
        parser_strategy=parser_strategy, structure_strategy=structure_strategy,
        toc_max_depth=toc_max_depth, subsplit=subsplit,
        index_granularity=index_granularity,
    )

    if preview:
        sid = standard_id_of(input_path, standard_id)
        with open(input_path, encoding="utf-8") as f:
            items = json.load(f)
        document = parser_factory.select(profile.parser_strategy).adapt(
            items, standard_id=sid, source_file=input_path.name)
        chunks = splitter_factory.select(profile.structure_strategy).split(
            document, max_depth=profile.toc_max_depth, subsplit=profile.subsplit).chunks
        console.print(f"[green]✓ 切分（{profile.structure_strategy}）："
                      f"{len(document.blocks)} 块 → {len(chunks)} 个节点[/green]")
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


@cli.command("parse")
@click.option("--input", "input_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="MinerU 输出的 *_content_list.json（阶段 0 缓存）。")
@click.option("--parser-strategy", default="mineru", show_default=True,
              help="解析模型（parser/ factory 键）。")
@_locate_opts
def cmd_parse(input_path: Path, parser_strategy: str, standard_id: str,
              profile_name: str, structured_dir: Path) -> None:
    """① 解析：content_list.json → document.json（格式归一）。"""
    sid = standard_id_of(input_path, standard_id)
    out = structured_out_dir(sid, profile_name, structured_dir)
    profile = ParseProfile(name=profile_name, parser_strategy=parser_strategy)
    step_parse(input_path, standard_id=sid, profile=profile, structured_out=out)


@cli.command("split")
@click.option("--structure-strategy", default="toc", show_default=True,
              help="切分策略（splitter/ factory 键）。")
@click.option("--toc-max-depth", type=int, default=None,
              help="切到第几级目录（1=章/2=节/3=条…）；不传=全目录深度。")
@click.option("--subsplit", type=click.Choice(["none", "number"]), default="none",
              show_default=True, help="目录层下按编号细分：none=镜像目录 / number=按编号细分。")
@_locate_opts
def cmd_split(structure_strategy: str, toc_max_depth: int | None, subsplit: str,
              standard_id: str, profile_name: str, structured_dir: Path) -> None:
    """② 切分：读 document.json → chunks.json (+ catalog_blocks.json)。"""
    sid = standard_id_of(None, standard_id)
    out = structured_out_dir(sid, profile_name, structured_dir)
    profile = ParseProfile(name=profile_name, structure_strategy=structure_strategy,
                           toc_max_depth=toc_max_depth, subsplit=subsplit)
    document = load_document(out)
    step_split(document, profile=profile, structured_out=out)


@cli.command("view")
@click.option("--index-granularity", type=click.Choice(["section", "clause", "paragraph"]),
              default="clause", show_default=True, help="粒度视图（view 选哪层 emit）。")
@_locate_opts
def cmd_view(index_granularity: str, standard_id: str, profile_name: str,
             structured_dir: Path) -> None:
    """③ 选粒度：读 chunks.json → units.json。"""
    sid = standard_id_of(None, standard_id)
    out = structured_out_dir(sid, profile_name, structured_dir)
    profile = ParseProfile(name=profile_name, index_granularity=index_granularity)
    chunks = load_chunks(out)
    step_view(chunks, profile=profile, structured_out=out)


@cli.command("feature")
@click.option("--features", default="", help="启用的表征 kind（逗号分隔）；空=profile 默认免费 4 项。")
@_locate_opts
def cmd_feature(features: str, standard_id: str, profile_name: str,
                structured_dir: Path) -> None:
    """④ 表征：读 units.json → features.json。"""
    sid = standard_id_of(None, standard_id)
    out = structured_out_dir(sid, profile_name, structured_dir)
    kinds = [k.strip() for k in features.split(",") if k.strip()]
    profile = ParseProfile(name=profile_name, **({"features": kinds} if kinds else {}))
    units = load_units(out)
    step_feature(units, profile=profile, structured_out=out)


@cli.command("index")
@click.option("--index-granularity", type=click.Choice(["section", "clause", "paragraph"]),
              default="clause", show_default=True, help="索引粒度（写入每行 granularity 字段）。")
@click.option("--store-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="索引存储目录，默认 data/vector_store/<std>/<profile>/。")
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--embed-url", default="http://localhost:8097", show_default=True)
@click.option("--embed-model-id", default="/model", show_default=True)
@click.option("--batch-size", default=64, show_default=True, help="嵌入批大小。")
@click.option("--bm25-only", is_flag=True, help="只建 BM25 + metadata，跳过向量索引。")
@_locate_opts
def cmd_index(index_granularity: str, store_dir: Path | None, milvus_host: str,
              milvus_port: int, embed_url: str, embed_model_id: str, batch_size: int,
              bm25_only: bool, standard_id: str, profile_name: str,
              structured_dir: Path) -> None:
    """⑤ 索引：读 units.json + features.json → BM25 + metadata（+ 可选 Milvus）。"""
    sid = standard_id_of(None, standard_id)
    out = structured_out_dir(sid, profile_name, structured_dir)
    store = (Path(store_dir) if store_dir is not None
             else store_out_dir(sid, profile_name))
    collection = collection_of(store)
    profile = ParseProfile(name=profile_name, index_granularity=index_granularity)
    units = load_units(out)
    features = load_features(out)
    step_index(units, features, profile=profile, store_dir=store, collection=collection,
               milvus_host=milvus_host, milvus_port=milvus_port, embed_url=embed_url,
               embed_model_id=embed_model_id, batch_size=batch_size, bm25_only=bm25_only)


if __name__ == "__main__":
    cli()
