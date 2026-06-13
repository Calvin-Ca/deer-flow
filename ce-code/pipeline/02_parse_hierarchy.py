"""层级化解析流水线（结构层）— 格式适配 → 标注 → 建节点树。

PRD §3.2 新模型（节点树 + 多表征 + 粒度视图）：
  格式适配  FormatAdapter — 纯格式转换（page 归一、HTML 表格解析、text_level 原样透传、
            block_idx 溯源），无结构语义
  标注     CatalogLabeler — 目录打标器：给每块打 catalog 标签（"目录" / 所属目录条目
            标题 / None）+ standard_id；text_level / block_idx 溯源随 FormatAdapter 透传，不解析条文号、不建树
  建树     TreeBuilder — 把标注块还原成**保留 parent/child 的节点树**（不再压平）；
            **条文号识别 + node_type + parent/child + 祖先链 + 引用图分型**作「固有事实」
            在此一次算定，落 nodes.json 作单一真值

结构层三件已各自独立成文件、本脚本只作编排 + CLI：格式适配 format_adapter.py、
目录打标 catalog_labeler.py、建树 tree_builder.py。

设计转向（2026-06-12）：废弃「强条 / 法律强制」机制。引用图（references.py）与祖先链
在建树器一次算定（PRD §3.1「固有事实」）；语气/条件/表格等「语义投影」归表征层
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
from pathlib import Path

import click
from rich.console import Console

console = Console()

ROOT = Path(__file__).resolve().parent.parent  # ce-code/
DEFAULT_OUTPUT = ROOT / "data" / "structured"

# parse_profile（配置契约，根模块）需要从 ROOT import，提前加进 path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parse_profile import ParseProfile  # noqa: E402  (流水线配置契约，PRD §3.2)
from format_adapter import FormatAdapter  # noqa: E402  (格式适配：MinerU v1 → 统一块 schema)
from catalog_labeler import CatalogLabeler  # noqa: E402  (目录打标器，阶段 1 标注)
from tree_builder import TreeBuilder  # noqa: E402  (建树器，阶段 1 建节点树)


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
        nodes = TreeBuilder(self.profile).apply(
            annotated, entries=axis.entries, source_file=source_file,
            version=version, effective_date=effective_date, status=status,
        )
        _write_json(nodes, self.out_dir / "nodes.json")

        # 表征层（reprs runner，T5/T8）与索引层（04_build_index.py，T4）后续单独跑，指向上述 nodes.json
        if self.profile.terminal_stage in ("reprs", "index"):
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
    type=click.Choice(["structure", "reprs", "index"]),
    default="structure", show_default=True,
    help="流水线终止阶段（02 只产到 structure/nodes.json；reprs/index 留待下游脚本接管）。",
)
@click.option(
    "--index-granularity",
    type=click.Choice(["section", "clause", "paragraph"]),
    default="clause", show_default=True,
    help="索引期粒度视图（仅记入 profile，供 04 选层 emit；不影响建树）。",
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
    index_granularity: str,
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
        index_granularity=index_granularity,
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
        chunks = TreeBuilder(profile).apply(annotated, entries=axis.entries)
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
