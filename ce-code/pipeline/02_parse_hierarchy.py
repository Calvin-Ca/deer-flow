"""层级化解析流水线（结构层）— 格式适配 → 标注 → 建节点树。

PRD §3.2 新模型（节点树 + 多表征 + 粒度视图）：
  格式适配  FormatAdapter — 纯格式转换（page 归一、HTML 表格解析、text_level 原样透传、
            block_idx 溯源），无结构语义
  标注     CatalogLabeler — 目录打标器：给每块打 catalog 标签（"目录" / 所属目录条目
            标题 / None）+ standard_id；text_level / block_idx 溯源随 FormatAdapter 透传，不解析条文号、不建树
  建树     GranularityAxis — 把标注块还原成**保留 parent/child 的节点树**（不再压平）；
            **条文号识别 + node_type + parent/child + 祖先链 + 引用图分型**作「固有事实」
            在此一次算定，落 nodes.json 作单一真值

设计转向（2026-06-12）：废弃「强条 / 法律强制」机制。引用图（references.py）与祖先链
在本结构层一次算定（PRD §3.1「固有事实」）；语气/条件/表格等「语义投影」归表征层
（T8）。阶段 0（MinerU）最贵，只跑一次缓存于 data/parsed/，本脚本从其缓存起读。

溯源：每个节点 provenance.block_idx 回指 data/parsed/ 原始块（不可变），改算法重派生
不必重跑 MinerU。

输入：data/parsed/<standard>/<mode>/<standard>_content_list.json
输出：data/structured/<standard>/<profile>/
        structure.json  —— 标注产物（每块带目录信息的扁平列表，调试用）
        nodes.json      —— 节点树（含 parent_id/children_ids + 引用图 + 祖先链），单一真值
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import click
from rich.console import Console

console = Console()

ROOT = Path(__file__).resolve().parent.parent  # ce-code/
DEFAULT_OUTPUT = ROOT / "data" / "structured"

# schema（单一真值契约）/ extract 需要从 ROOT import，提前加进 path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema  # noqa: E402  (ce-code 根模块，节点契约)
from extract import references  # noqa: E402  (引用图分型 + 反向边，纯 stdlib)
from format_adapter import FormatAdapter  # noqa: E402  (格式适配：MinerU v1 → 统一块 schema)
from catalog_labeler import CatalogLabeler  # noqa: E402  (目录打标器，阶段 1 标注)


# ---------------------------------------------------------------------------
# ParseProfile — 流水线配置（对应 PRD §3.2 parse_profile 配置块）
# ---------------------------------------------------------------------------

@dataclass
class ParseProfile:
    """解析流水线配置：控制终止阶段、建树粒度、chunk 切法、增强深度。

    参数：
        name (str): 配置名，作为产物子目录名（data/structured/{standard}/{name}/）。
        terminal_stage (str): 流水线终止阶段：
            structure   仅建树（阶段 1），产出 structure.json
            granularity 建树 + 粒度化（阶段 2），产出 chunks.json
            enrich      建树 + 粒度化 + 增强（阶段 3），产出 clauses.json（默认）
            index       继续到阶段 4（调 04_build_index.py，暂未实现）
        structure_depth (str): 阶段 1 建树到哪层 — section / clause / subitem
        chunk_granularity (str): 阶段 2 切分粒度 — node / paragraph / natural
        enrichment (str): 阶段 3 增强深度 — none / ids_refs / full
        small_to_big (bool): 是否在 chunk 中拼入祖先上下文（small-to-big 策略）
    返回：
        无（dataclass，实例化后传入 run_pipeline）。
    """

    name: str = "default"
    terminal_stage: Literal["structure", "granularity", "enrich", "index"] = "enrich"
    structure_depth: Literal["section", "clause", "subitem"] = "clause"
    chunk_granularity: Literal["node", "paragraph", "natural"] = "natural"
    enrichment: Literal["none", "ids_refs", "full"] = "full"
    small_to_big: bool = True


# ---------------------------------------------------------------------------
# 条文号正则（建树层用：GranularityAxis 从标题文字识别条款号）
# ---------------------------------------------------------------------------

CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[　 一-鿿]")
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")
APPENDIX_CLAUSE_RE = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,2})\s*[一-鿿]")

# ---------------------------------------------------------------------------
# node_type 推断（建树层：由条款路径号段数定章/节/条，对应 PRD §3.1 节点 schema）
# ---------------------------------------------------------------------------

_APPENDIX_ROOT_RE = re.compile(r"^附录\s*[A-Z]$")


def _infer_node_type(path: str) -> str:
    """由条款路径推断 node_type（层级 = 号段数，自包含，不依赖标题栈）。

    参数：
        path (str): 条款号，如 "1" / "5.3.4" / "附录E" / "E.1.1"。
    返回：
        str: node_type 枚举值 — chapter / section / clause / appendix。
            层级按小数点号段数：1 段→chapter、2 段→section、≥3 段→clause；
            "附录X" 整根为 appendix。paragraph / table / figure / formula 由建树层
            按元素类型在挂载点赋值。
    """
    if _APPENDIX_ROOT_RE.match(path):
        return "appendix"
    level = path.count(".") + 1
    if level == 1:
        return "chapter"
    if level == 2:
        return "section"
    return "clause"


# ---------------------------------------------------------------------------
# 条文号 → 类型/置信度（建树层：GranularityAxis 调用）
# ---------------------------------------------------------------------------


def classify_heading(text: str) -> dict | None:
    """从一行标题文字识别条款号 / 类型 / 置信度 —— **无状态纯函数**（建树层调用）。

    功能：把「这行标题对应哪个条款号、是什么 node_type、路径来源置信几何」这件纯文本
        判定独立出来，便于单测与复用。node_type 由条款路径号段数自推（_infer_node_type），
        不依赖标题栈/外部层级。

    参数：
        text (str): 标题块文字（调用方已判定 text_level 存在，即 MinerU 标题块）。
    返回：
        dict | None: ``{clause_path, node_type, path_source, path_confidence}``。
            返回 None 表示该行实为交叉引用片段（如「5.3节…」），应按内容块处理。
            ``path_source``：number（命中编号正则，置信 1.0）/ text_level（无编号、
            靠 MinerU 标题标记 + 标题文字兜底作路径，置信 0.6）。
    """
    # 附录根（附录A / 附录B）
    app_m = APPENDIX_RE.match(text)
    if app_m:
        return {"clause_path": f"附录{app_m.group(1)}", "node_type": "appendix",
                "path_source": "number", "path_confidence": 1.0}

    # 附录字母条号（E.1 / E.2.2）
    appc_m = APPENDIX_CLAUSE_RE.match(text)
    if appc_m:
        path = appc_m.group(1)
        return {"clause_path": path, "node_type": _infer_node_type(path),
                "path_source": "number", "path_confidence": 1.0}

    # 本规范条号（5 / 5.3 / 5.3.4）
    m = CLAUSE_NUM_RE.match(text)
    if m:
        num = m.group(1)
        # "节条款项"后缀说明这是交叉引用片段，非真实条号
        if text[len(num):len(num) + 1] in "节条款项":
            return None
        return {"clause_path": num, "node_type": _infer_node_type(num),
                "path_source": "number", "path_confidence": 1.0}

    # 无编号标题（"前言"、"术语和定义" 等）：用标题文字作路径
    path = text[:30].strip()
    return {"clause_path": path, "node_type": _infer_node_type(path),
            "path_source": "text_level", "path_confidence": 0.6}


# ---------------------------------------------------------------------------
# 父路径推断（建树用：由 clause_path 反推父节点路径）
# ---------------------------------------------------------------------------

_NUMERIC_PATH_RE = re.compile(r"^\d+(?:\.\d+)*$")        # 5 / 5.3 / 5.3.4
_APPENDIX_CLAUSE_PATH_RE = re.compile(r"^[A-Z](?:\.\d+)+$")  # E.1 / E.2.2


def _parent_path(path: str) -> str | None:
    """由条款路径反推其父节点的条款路径（仅按编号，不查节点是否存在）。

    参数：
        path (str): 条款号，如 "5.3.4" / "5" / "附录E" / "E.1.1" / "前言"。
    返回：
        str | None: 父节点条款路径；顶层节点（章 / 附录根 / 无编号标题）返回 None。
            "5.3.4"→"5.3"，"5"→None，"E.1.1"→"E.1"，"E.1"→"附录E"，"附录E"→None。
    """
    if _APPENDIX_ROOT_RE.match(path):           # 附录E → 顶层
        return None
    if _APPENDIX_CLAUSE_PATH_RE.match(path):    # E.1.1 → E.1；E.1 → 附录E
        parent = path.rsplit(".", 1)[0]
        return f"附录{parent}" if "." not in parent else parent
    if _NUMERIC_PATH_RE.match(path):            # 5.3.4 → 5.3；5 → 顶层
        return path.rsplit(".", 1)[0] if "." in path else None
    return None                                 # 标题路径节点（前言 / 术语…）顶层


def _resolve_parent(path: str, by_path: dict[str, dict]) -> str | None:
    """解析最近的**已存在**祖先路径（中间层级缺节点时继续上探）。

    参数：
        path (str): 当前节点条款路径。
        by_path (dict): clause_path → 节点 的映射。
    返回：
        str | None: 最近存在的祖先条款路径；无则 None（顶层）。
    """
    p = _parent_path(path)
    while p is not None and p not in by_path:
        p = _parent_path(p)
    return p


# ---------------------------------------------------------------------------
# 建树 — GranularityAxis（结构层：标注块 → 保留 parent/child 的节点树）
# ---------------------------------------------------------------------------


class GranularityAxis:
    """结构层建树：把标注块还原成**保留 parent/child 的节点树**（单一真值）。

    功能：
        ① 按标题分组聚合成节点（schema.Node），收集正文 / 表格 / 图示 / 溯源块号；
        ② 由 clause_path 反推 parent_id、回填 children_ids（不再压平）；
        ③ 沿父链算定祖先链（ancestor_titles / ancestor_paths）；
        ④ 调 references.annotate_references 算定引用图分型 + 反向边。
        ②③④ 是 PRD §3.1 的「固有事实」——建树时一次算定，唯一、不可多表达。
        表格 / 图示暂留节点上 tables / images 字段（过渡），T8 转表征层子节点 +
        table_struct 表征。

    参数：
        profile (ParseProfile): 解析配置（读 chunk_granularity；非 node 暂退回 node）。
    返回：
        调用 apply(annotated, ...) 返回节点树 list[Node]。
    """

    def __init__(self, profile: ParseProfile) -> None:
        """初始化建树器。

        参数：
            profile (ParseProfile): 解析配置。
        返回：
            无。
        """
        self.profile = profile

    def apply(
        self,
        annotated: list[dict],
        *,
        source_file: str = "",
        version: str = "",
        effective_date: str = "",
        status: str = "active",
    ) -> list[dict]:
        """标注块 → 节点树（含 parent/child + 引用图 + 祖先链）。

        参数：
            annotated (list[dict]): CatalogLabeler.annotate() 产出的标注列表。
            source_file (str): 原始 content_list.json 路径（写入 provenance 溯源）。
            version / effective_date / status (str): 规范级元数据，写入每个节点。
        返回：
            list[dict]: 节点树（schema.Node 形态）。
        """
        if self.profile.chunk_granularity != "node":
            console.print(
                f"[yellow]⚠ chunk_granularity={self.profile.chunk_granularity!r} 暂未实现，"
                f"退回 node 粒度建树[/yellow]"
            )
        nodes = self._group_into_nodes(
            annotated, source_file=source_file,
            version=version, effective_date=effective_date, status=status,
        )
        self._wire_tree(nodes)
        self._attach_ancestors(nodes)
        references.annotate_references(nodes)  # 固有事实：引用图分型 + referenced_by 反向边
        return nodes

    @staticmethod
    def _group_into_nodes(
        annotated: list[dict],
        *,
        source_file: str,
        version: str,
        effective_date: str,
        status: str,
    ) -> list[dict]:
        """标注块按标题分组，聚合成 schema.Node 节点（未连边）。

        功能：
            **条文号识别在此一次算定**（下沉自目录打标器）：对有 text_level（MinerU 标题）的块跑
            classify_heading 提 clause_path / node_type / path_source / path_confidence，
            命中即开新节点；返回 None（交叉引用片段如「5.3节…」）则按内容块处理。
            非标题块追加进当前节点正文；表格 / 图示暂存节点 tables / images 字段
            （过渡，T8 转子节点）；逐块累积 block_idx / page 进 provenance。
            catalog=="目录" 的目录页块不开节点、不并入正文（structure.json 已全量保留，不算减法）。
            首个标题之前的内容块（无归属）丢弃；空节点（无正文且无表格）丢弃。
            level 由 clause_path 号段数推导（new_node）。

        参数：
            annotated (list[dict]): CatalogLabeler.annotate() 产出的标注列表
                （每块带 text_level / catalog / standard_id 等标签）。
            source_file (str): provenance.source_file。
            version / effective_date / status (str): 规范级元数据。
        返回：
            list[dict]: schema.Node 节点列表（parent_id/children_ids/祖先链/引用图待补）。
        """
        nodes: list[dict] = []
        cur: dict | None = None

        def _flush() -> None:
            if cur and (cur["content"].strip() or cur["tables"]):
                prov = cur["provenance"]
                prov["block_idx"] = sorted(set(prov["block_idx"]))
                prov["page"] = sorted(set(prov["page"]))
                nodes.append(cur)

        for elem in annotated:
            if elem.get("catalog") == "目录":
                continue  # 目录页块不开节点、不并入正文（structure.json 已全量保留 + 溯源）

            info = classify_heading(elem.get("text", "")) if elem.get("text_level") is not None else None
            if info is not None:
                _flush()
                cur = schema.new_node(
                    elem.get("standard_id", ""),
                    info["clause_path"],
                    info["node_type"],
                    title=elem["text"],
                    page=elem["page"],
                    version=version,
                    effective_date=effective_date,
                    status=status,
                    path_source=info["path_source"],
                    path_confidence=info["path_confidence"],
                    provenance={
                        "source_file": source_file,
                        "block_idx": [elem["block_idx"]] if "block_idx" in elem else [],
                        "page": [elem["page"]],
                    },
                )
                # 过渡字段：表格 / 图示暂挂节点上，T8 转表征层子节点 + table_struct
                cur["tables"] = []
                cur["images"] = []
                continue

            if cur is None:
                continue  # 首个标题之前的内容块，无归属节点 → 丢弃

            t = elem.get("type")
            if "block_idx" in elem:
                cur["provenance"]["block_idx"].append(elem["block_idx"])
            cur["provenance"]["page"].append(elem["page"])
            if t == "table":
                cur["tables"].append({
                    "caption": elem.get("text", ""),
                    "body": elem.get("body", []),
                    "page": elem["page"],
                })
            elif t == "image":
                cur["images"].append({
                    "path": elem.get("img_path", ""),
                    "caption": elem.get("text", ""),
                    "page": elem["page"],
                })
            else:
                text = elem.get("text", "")
                if text:
                    cur["content"] = (cur["content"] + "\n" + text).lstrip("\n")

        _flush()
        return nodes

    @staticmethod
    def _wire_tree(nodes: list[dict]) -> None:
        """原地连边：由 clause_path 反推 parent_id，并回填父节点 children_ids。

        参数：
            nodes (list[dict]): _group_into_nodes 产出的节点列表（按文档序）。
        返回：
            无（原地写 parent_id / children_ids）。
        """
        by_path: dict[str, dict] = {}
        for n in nodes:
            by_path.setdefault(n["clause_path"], n)  # 同号取首现，重复不覆盖
        for n in nodes:
            parent_path = _resolve_parent(n["clause_path"], by_path)
            parent = by_path.get(parent_path) if parent_path is not None else None
            n["parent_id"] = parent["node_id"] if parent is not None else None
            if parent is not None:
                parent["children_ids"].append(n["node_id"])

    @staticmethod
    def _attach_ancestors(nodes: list[dict]) -> None:
        """原地算定祖先链：沿 parent_id 上溯，写 ancestor_titles / ancestor_paths（不含自身）。

        参数：
            nodes (list[dict]): 已连边（含 parent_id）的节点列表。
        返回：
            无（原地写 ancestor_titles / ancestor_paths，章→直接父，自顶向下）。
        """
        by_id = {n["node_id"]: n for n in nodes}
        for n in nodes:
            titles: list[str] = []
            paths: list[str] = []
            cur = by_id.get(n["parent_id"]) if n["parent_id"] else None
            seen: set[str] = set()
            while cur is not None and cur["node_id"] not in seen:
                seen.add(cur["node_id"])
                titles.append(cur["title"])
                paths.append(cur["clause_path"])
                cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
            n["ancestor_titles"] = titles[::-1]
            n["ancestor_paths"] = paths[::-1]


# ---------------------------------------------------------------------------
# 流水线编排器
# ---------------------------------------------------------------------------


class Pipeline:
    """结构层解析流水线编排器：标注 → 建节点树。

    功能：
        执行标注（structure.json，调试用）与建树（nodes.json，单一真值）两步，
        产物写入 out_dir。表征层（reprs，T5/T8）与索引层（04_build_index.py）不在此。

    参数：
        standard_id (str): 规范唯一标识（目录名的一部分）。
        profile (ParseProfile): 解析配置。
        out_root (Path): 产物根目录（data/structured/）。
    返回：
        调用 run(elements, ...) 返回节点树。
    """

    def __init__(self, standard_id: str, profile: ParseProfile, out_root: Path) -> None:
        """初始化流水线，确定产物目录。

        参数：
            standard_id (str): 规范唯一标识。
            profile (ParseProfile): 解析配置。
            out_root (Path): 产物根目录。
        返回：
            无。
        """
        self.standard_id = standard_id
        self.profile = profile
        safe_std = re.sub(r"[^\w\-]", "_", standard_id)
        self.out_dir = out_root / safe_std / profile.name

    def run(
        self,
        elements: list[dict],
        *,
        source_file: str = "",
        version: str = "",
        effective_date: str = "",
        status: str = "active",
    ) -> list[dict]:
        """执行结构层流水线：标注 → 建树，写 structure.json + nodes.json。

        参数：
            elements (list[dict]): FormatAdapter.adapt() 产出的统一元素列表。
            source_file (str): 原始 content_list.json 路径（写入节点 provenance）。
            version / effective_date / status: 规范元数据，写入每个节点。
        返回：
            list[dict]: 节点树（terminal_stage=structure 时返回标注块）。
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # 标注：给每块追加目录上下文（调试中间产物）
        console.print(f"[bold cyan]标注[/bold cyan] → {self.out_dir / 'structure.json'}")
        axis = CatalogLabeler(self.standard_id)
        annotated = axis.annotate(elements)
        _write_json(annotated, self.out_dir / "structure.json")
        axis.print_stats(annotated)

        if self.profile.terminal_stage == "structure":
            return annotated

        # 建树：还原 parent/child 节点树 + 固有事实（引用图 / 祖先链），落单一真值
        console.print(f"[bold cyan]建节点树[/bold cyan] → {self.out_dir / 'nodes.json'}")
        nodes = GranularityAxis(self.profile).apply(
            annotated, source_file=source_file,
            version=version, effective_date=effective_date, status=status,
        )
        _write_json(nodes, self.out_dir / "nodes.json")

        # 表征层（reprs，T5/T8）与索引层（04_build_index.py）后续单独跑，指向上述 nodes.json
        if self.profile.terminal_stage in ("enrich", "index"):
            console.print(
                "[yellow]表征层/索引待 T5/T8 + 04 接管；本步只产出结构层 nodes.json[/yellow]"
            )

        return nodes


def _write_json(data: list, path: Path) -> None:
    """把 list 序列化为 JSON 写入 path。

    参数：
        data (list): 待写数据。
        path (Path): 目标路径（父目录须已存在）。
    返回：
        无。
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 写入 {path}（{len(data)} 条）[/green]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--input", "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="MinerU 输出的 _content_list.json（v1）路径。",
)
@click.option("--standard-id", default="", help="规范标识；不传则取输入文件名 basename。")
@click.option(
    "--output-dir", "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
    show_default=True,
)
@click.option("--profile-name", default="default", show_default=True, help="parse_profile 名（产物子目录）。")
@click.option(
    "--terminal-stage",
    type=click.Choice(["structure", "granularity", "enrich", "index"]),
    default="enrich", show_default=True,
    help="流水线终止阶段。",
)
@click.option(
    "--chunk-granularity",
    type=click.Choice(["node", "paragraph", "natural"]),
    default="natural", show_default=True,
)
@click.option(
    "--enrichment",
    type=click.Choice(["none", "ids_refs", "full"]),
    default="full", show_default=True,
)
@click.option("--version", default="", help="规范版本，如 2018。")
@click.option("--effective-date", default="", help="生效日期，如 2018-10-01。")
@click.option("--status", default="active", help="active / superseded / abolished。")
@click.option("--preview", is_flag=True, help="只打印前 20 条节点，不写文件。")
def main(
    input_path: Path,
    standard_id: str,
    output_dir: Path,
    profile_name: str,
    terminal_stage: str,
    chunk_granularity: str,
    enrichment: str,
    version: str,
    effective_date: str,
    status: str,
    preview: bool,
) -> None:
    """层级化解析（结构层）：从 MinerU content_list.json 建节点树。

    产物路径：data/structured/<standard>/<profile>/
      structure.json  —— 标注中间产物（调试用）
      nodes.json      —— 节点树（parent/child + 引用图 + 祖先链），单一真值
    """
    base = input_path.stem.replace("_content_list", "")
    if not standard_id:
        standard_id = base

    profile = ParseProfile(
        name=profile_name,
        terminal_stage=terminal_stage,
        chunk_granularity=chunk_granularity,
        enrichment=enrichment,
    )

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]输入：[/bold]{input_path}")
    console.print(f"[bold]Profile：[/bold]{profile.name}  terminal_stage={profile.terminal_stage}")

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    elements = FormatAdapter.adapt(data)
    console.print(f"共 {len(elements)} 个原始元素（page_number 已过滤），开始解析…")

    if preview:
        axis = CatalogLabeler(standard_id)
        annotated = axis.annotate(elements)
        chunks = GranularityAxis(profile).apply(annotated)
        console.print(f"[green]✓ 标注 {len(annotated)} 块，聚合为 {len(chunks)} 个节点[/green]")
        axis.print_stats(annotated)
        console.print("\n[bold]--- 前 20 条节点预览 ---[/bold]")
        for c in chunks[:20]:
            tables = f"  [{len(c['tables'])}表]" if c.get("tables") else ""
            console.print(
                f"  [cyan]{c['clause_path']}[/cyan] [{c.get('node_type', '?')}]"
                f" (p{c['page']}) {c['title'][:50]}{tables}"
            )
        return

    # provenance.source_file：尽量记为相对 data/parsed 的路径，回指阶段 0 缓存
    try:
        source_file = str(input_path.resolve().relative_to(ROOT / "data" / "parsed"))
    except ValueError:
        source_file = input_path.name

    Pipeline(standard_id, profile, output_dir).run(
        elements,
        source_file=source_file,
        version=version,
        effective_date=effective_date,
        status=status,
    )


if __name__ == "__main__":
    main()
