"""阶段 0 第三步：审核条款树提取质量。

用途：
  1. 详细统计报告（层级分布、强条分布、引用密度、空内容率）
  2. 快速抽查指定条款 --show-clause 5.3.1
  3. 自动检测常见提取问题（空内容、疑似误标强条、孤立条款）
  4. 导出 Markdown 质量报告

输入：data/structured/<standard>_clauses.json
输出：终端报告 or data/quality_reports/<standard>_report.md
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_DIR = ROOT / "data" / "quality_reports"

# 推荐性用词（非强条关键词）
ADVISORY_RE = re.compile(r"宜|可(?!燃)|允许|建议")
# 页码噪声模式（如"6/16"、"第6页"）
PAGE_NOISE_RE = re.compile(r"^\d+/\d+$|^第\s*\d+\s*[页面]$")


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_clauses(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def compute_stats(clauses: list[dict]) -> dict[str, Any]:
    total = len(clauses)
    mandatory = [c for c in clauses if c.get("is_mandatory")]
    with_tables = [c for c in clauses if c.get("tables")]
    with_images = [c for c in clauses if c.get("images")]
    with_refs = [c for c in clauses if c.get("references_to")]
    empty_content = [c for c in clauses if not c.get("content", "").strip()]

    by_level: dict[int, list] = {}
    for c in clauses:
        lvl = c.get("level", 0)
        by_level.setdefault(lvl, []).append(c)

    # 强条按章分布
    mandatory_by_chapter: dict[str, int] = {}
    for c in mandatory:
        path = c.get("node_path", "")
        chapter = path.split(".")[0] if "." in path else path
        mandatory_by_chapter[chapter] = mandatory_by_chapter.get(chapter, 0) + 1

    return {
        "total": total,
        "mandatory": mandatory,
        "with_tables": with_tables,
        "with_images": with_images,
        "with_refs": with_refs,
        "empty_content": empty_content,
        "by_level": by_level,
        "mandatory_by_chapter": mandatory_by_chapter,
    }


def print_stats(stats: dict[str, Any]) -> None:
    total = stats["total"]

    t = Table(title="条款树统计", show_header=True, header_style="bold cyan")
    t.add_column("指标", style="white")
    t.add_column("数量", justify="right", style="yellow")
    t.add_column("占比", justify="right", style="dim")

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "-"

    t.add_row("总条款数", str(total), "")
    t.add_row("强制性条款", str(len(stats["mandatory"])), pct(len(stats["mandatory"])))
    t.add_row("含表格", str(len(stats["with_tables"])), pct(len(stats["with_tables"])))
    t.add_row("含图示", str(len(stats["with_images"])), pct(len(stats["with_images"])))
    t.add_row("含交叉引用", str(len(stats["with_refs"])), pct(len(stats["with_refs"])))
    t.add_row("[red]空内容条款[/red]", str(len(stats["empty_content"])), pct(len(stats["empty_content"])))
    console.print(t)

    # 层级分布
    t2 = Table(title="层级分布", header_style="bold cyan")
    t2.add_column("层级")
    t2.add_column("条款数", justify="right")
    t2.add_column("示例", style="dim")
    for lvl in sorted(stats["by_level"]):
        examples = [c["node_path"] for c in stats["by_level"][lvl][:3]]
        t2.add_row(f"第 {lvl} 层", str(len(stats["by_level"][lvl])), "  ".join(examples))
    console.print(t2)

    # 强条按章分布
    if stats["mandatory_by_chapter"]:
        t3 = Table(title="强条按章分布", header_style="bold cyan")
        t3.add_column("章")
        t3.add_column("强条数", justify="right")
        for ch, cnt in sorted(stats["mandatory_by_chapter"].items(), key=lambda x: x[1], reverse=True):
            t3.add_row(f"第 {ch} 章", str(cnt))
        console.print(t3)


# ---------------------------------------------------------------------------
# 问题检测
# ---------------------------------------------------------------------------

def detect_issues(clauses: list[dict]) -> list[dict]:
    issues = []

    all_paths = {c["node_path"] for c in clauses}

    for c in clauses:
        path = c.get("node_path", "")
        content = c.get("content", "").strip()
        is_mandatory = c.get("is_mandatory", False)
        refs = c.get("references_to", [])

        # 1. 空内容
        if not content:
            issues.append({"type": "empty_content", "clause": path, "detail": "条款正文为空"})

        # 2. 内容极短（可能是提取不完整）
        elif len(content) < 10:
            issues.append({"type": "short_content", "clause": path, "detail": f"正文仅 {len(content)} 字：{content!r}"})

        # 3. 页码噪声
        for line in content.split("\n"):
            if PAGE_NOISE_RE.match(line.strip()):
                issues.append({"type": "page_noise", "clause": path, "detail": f"疑似页码噪声行：{line.strip()!r}"})
                break

        # 4. 强条中只含推荐性用词（may be over-flagged）
        if is_mandatory and not re.search(r"必须|严禁|不应|不得|禁止|应", content):
            issues.append({"type": "suspicious_mandatory", "clause": path, "detail": "标记为强条但未见强条关键词"})

        # 5. 非强条但含强条关键词（可能漏标）
        if not is_mandatory and re.search(r"必须|严禁|不得|禁止", content):
            issues.append({"type": "missed_mandatory", "clause": path,
                           "detail": f"含强条关键词但未标记强条：{re.search(r'必须|严禁|不得|禁止', content).group()!r}"})

        # 6. 引用了不存在的条款
        for ref in refs:
            if ref not in all_paths:
                issues.append({"type": "dangling_ref", "clause": path, "detail": f"引用 {ref} 不在条款库中"})

    return issues


def print_issues(issues: list[dict]) -> None:
    if not issues:
        console.print("[green]✓ 未发现明显提取问题[/green]")
        return

    by_type: dict[str, list] = {}
    for iss in issues:
        by_type.setdefault(iss["type"], []).append(iss)

    labels = {
        "empty_content": ("red", "空内容"),
        "short_content": ("yellow", "内容过短"),
        "page_noise": ("yellow", "页码噪声"),
        "suspicious_mandatory": ("magenta", "强条疑似误标"),
        "missed_mandatory": ("red", "强条可能漏标"),
        "dangling_ref": ("cyan", "悬空引用"),
    }

    for itype, items in by_type.items():
        color, label = labels.get(itype, ("white", itype))
        console.print(f"\n[{color}][{label}] {len(items)} 条[/{color}]")
        for iss in items[:10]:
            console.print(f"  {iss['clause']}: {iss['detail']}")
        if len(items) > 10:
            console.print(f"  … 还有 {len(items) - 10} 条")


# ---------------------------------------------------------------------------
# 单条款查看
# ---------------------------------------------------------------------------

def show_clause_detail(clauses: list[dict], node_path: str) -> None:
    hits = [c for c in clauses if c.get("node_path") == node_path]
    if not hits:
        # 前缀匹配（如输入"5.3"返回5.3.x）
        hits = [c for c in clauses if c.get("node_path", "").startswith(node_path + ".")]
    if not hits:
        console.print(f"[red]未找到条款 {node_path}[/red]")
        return

    for c in hits:
        mandatory_tag = " [red][强条][/red]" if c.get("is_mandatory") else " [dim][推荐][/dim]"
        title = f"[bold cyan]{c['node_path']}[/bold cyan]{mandatory_tag}  p{c.get('page','?')}"
        body = c.get("content", "(空)") or "(空)"
        refs = c.get("references_to", [])
        tables = c.get("tables", [])

        ref_str = f"\n[dim]→ 引用：{', '.join(refs)}[/dim]" if refs else ""
        tbl_str = f"\n[dim]→ 表格：{len(tables)} 张[/dim]" if tables else ""

        console.print(Panel(
            Text.from_markup(f"{body}{ref_str}{tbl_str}"),
            title=title,
            border_style="cyan",
        ))


# ---------------------------------------------------------------------------
# Markdown 报告导出
# ---------------------------------------------------------------------------

def write_report(clauses: list[dict], stats: dict, issues: list[dict], out_path: Path, standard_id: str) -> None:
    lines = [
        f"# 条款提取质量报告：{standard_id}\n",
        f"**总条款数**：{stats['total']}  \n",
        f"**强制性条款**：{len(stats['mandatory'])}  \n",
        f"**含表格**：{len(stats['with_tables'])}  \n",
        f"**含图示**：{len(stats['with_images'])}  \n",
        f"**含交叉引用**：{len(stats['with_refs'])}  \n",
        f"**空内容条款**：{len(stats['empty_content'])}  \n",
        "\n## 层级分布\n",
        "| 层级 | 数量 |\n|---|---|\n",
    ]
    for lvl in sorted(stats["by_level"]):
        lines.append(f"| 第 {lvl} 层 | {len(stats['by_level'][lvl])} |\n")

    lines += ["\n## 强条按章分布\n", "| 章 | 强条数 |\n|---|---|\n"]
    for ch, cnt in sorted(stats["mandatory_by_chapter"].items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| 第 {ch} 章 | {cnt} |\n")

    lines.append("\n## 发现的问题\n")
    if not issues:
        lines.append("无明显问题。\n")
    else:
        by_type: dict[str, list] = {}
        for iss in issues:
            by_type.setdefault(iss["type"], []).append(iss)
        label_map = {
            "empty_content": "空内容",
            "short_content": "内容过短",
            "page_noise": "页码噪声",
            "suspicious_mandatory": "强条疑似误标",
            "missed_mandatory": "强条可能漏标",
            "dangling_ref": "悬空引用",
        }
        for itype, items in by_type.items():
            lines.append(f"\n### {label_map.get(itype, itype)}（{len(items)} 条）\n\n")
            for iss in items:
                lines.append(f"- `{iss['clause']}`: {iss['detail']}\n")

    lines.append("\n## 强条列表（前50条）\n\n")
    for c in stats["mandatory"][:50]:
        snippet = (c.get("content") or "")[:80].replace("\n", " ")
        lines.append(f"- **{c['node_path']}** (p{c.get('page','?')}): {snippet}…\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(lines), encoding="utf-8")
    console.print(f"[green]✓ 报告已写入 {out_path}[/green]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--input", "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="data/structured/<standard>_clauses.json",
)
@click.option("--show-clause", default="", help="查看指定条款号，如 5.3.1（支持前缀匹配）。")
@click.option("--check-issues", is_flag=True, help="运行自动问题检测。")
@click.option("--export-report", is_flag=True, help="导出 Markdown 质量报告到 data/quality_reports/。")
@click.option("--standard-id", default="", help="规范标识（默认从文件名推断）。")
def main(
    input_path: Path,
    show_clause: str,
    check_issues: bool,
    export_report: bool,
    standard_id: str,
) -> None:
    """审核条款树提取质量：统计、问题检测、单条款查看、报告导出。"""

    if not standard_id:
        standard_id = (
            input_path.stem
            .replace("_clauses", "")
            .replace("_", " ")
            .strip()
        )

    console.print(f"[bold]规范：[/bold]{standard_id}")
    clauses = load_clauses(input_path)
    console.print(f"加载 {len(clauses)} 条条款")

    stats = compute_stats(clauses)

    if show_clause:
        show_clause_detail(clauses, show_clause)
        return

    print_stats(stats)

    if check_issues or export_report:
        issues = detect_issues(clauses)
        print_issues(issues)

        if export_report:
            safe = standard_id.replace(" ", "_").replace("(", "").replace(")", "")
            out_path = DEFAULT_REPORT_DIR / f"{safe}_report.md"
            write_report(clauses, stats, issues, out_path, standard_id)
    else:
        console.print("\n[dim]提示：加 --check-issues 运行自动问题检测，加 --export-report 导出报告[/dim]")


if __name__ == "__main__":
    main()
