"""阶段 0 第二步：从 MinerU 的 _content_list_v2.json 构建条款树。

输入：data/parsed/<standard>/<mode>/<standard>_content_list_v2.json
输出：data/structured/<standard>_clauses.json

条款树结构：章 → 节 → 条 → 款，每个叶子节点带完整元数据。
每条款输出格式见 CLAUDE.md §4.1。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "structured"

# ---------------------------------------------------------------------------
# 正则模式
# ---------------------------------------------------------------------------

# 条款编号：1 / 1.1 / 1.1.1 / 1.1.1.1（行首，后跟空白或中文）
CLAUSE_NUM_RE = re.compile(
    r"^(\d+(?:\.\d+){0,3})\s+[一-鿿\w]"
)

# 附录编号：附录A / 附录 B
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")

# 强制性关键词（出现即标记 is_mandatory=True）
MANDATORY_RE = re.compile(r"必须|严禁|不应|不得|禁止")

# 推荐性关键词（用于区分强条中的"应"）
# 注："应"单独判断强制，但"宜/可"明确非强制
RECOMMENDED_RE = re.compile(r"[宜]|^可[以]?")

# 交叉引用：匹配"符合 X.X.X 的规定/要求"等
REFERENCE_RE = re.compile(
    r"(?:符合|按|按照|执行|见|参见|参照)\s*"
    r"(?:本[规标]范?\s*)?"
    r"第?\s*(\d+\.\d+(?:\.\d+)*)\s*(?:条|款|节)?"
    r"(?:\s*(?:的规定|的要求|执行))?"
)

# ---------------------------------------------------------------------------
# 文本提取工具
# ---------------------------------------------------------------------------

def extract_text(elem: dict) -> str:
    """从任意元素中提取纯文本。"""
    t = elem.get("type", "")
    c = elem.get("content", {})
    if t == "title":
        parts = c.get("title_content", [])
    elif t == "paragraph":
        parts = c.get("paragraph_content", [])
    elif t == "table":
        # 表格单独处理，这里只返回 caption
        caps = c.get("table_caption", [])
        return " ".join(x.get("content", "") for x in caps if x.get("type") == "text").strip()
    elif t == "image":
        caps = c.get("image_caption", [])
        return " ".join(x.get("content", "") for x in caps if x.get("type") == "text").strip()
    else:
        return ""
    return " ".join(x.get("content", "") for x in parts if x.get("type") == "text").strip()


def extract_table_body(elem: dict) -> list[list[str]]:
    """把 table 元素的 table_body 转成二维字符串列表。"""
    c = elem.get("content", {})
    rows = []
    for row in c.get("table_body", []):
        cells = []
        for cell in row:
            cell_text = ""
            for item in cell.get("cell_content", []):
                for sub in item.get("paragraph_content", item.get("title_content", [])):
                    cell_text += sub.get("content", "")
            cells.append(cell_text.strip())
        rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# 条款层级判断
# ---------------------------------------------------------------------------

def clause_level(num_str: str) -> int:
    """'1' → 1, '1.1' → 2, '1.1.1' → 3, '1.1.1.1' → 4"""
    return len(num_str.split("."))


def is_mandatory(text: str) -> bool:
    return bool(MANDATORY_RE.search(text))


def extract_references(text: str) -> list[str]:
    return list(dict.fromkeys(REFERENCE_RE.findall(text)))  # 去重保序


# ---------------------------------------------------------------------------
# 核心解析
# ---------------------------------------------------------------------------

def parse_content_list(pages: list[list[dict]], standard_id: str) -> list[dict]:
    """
    把 content_list_v2 的页面列表解析成扁平条款列表。
    每条款是 CLAUDE.md §4.1 定义的 dict。
    """
    clauses: list[dict] = []
    # 当前层级栈：{level: clause_dict}，用于把内容挂到正确的父节点
    stack: dict[int, dict] = {}

    def current_clause() -> dict | None:
        if not stack:
            return None
        return stack[max(stack)]

    def flush_clause(clause: dict) -> None:
        """整理并追加到 clauses 列表。"""
        text = clause.get("content", "").strip()
        if not text and not clause.get("tables"):
            return
        clause["is_mandatory"] = is_mandatory(text)
        clause["references_to"] = extract_references(text)
        clauses.append(clause)

    for page_idx, page in enumerate(pages):
        for elem in page:
            text = extract_text(elem)
            if not text and elem.get("type") != "table":
                continue

            # --- 判断是否是新条款标题 ---
            m = CLAUSE_NUM_RE.match(text)
            app_m = APPENDIX_RE.match(text)

            if m:
                num = m.group(1)
                lvl = clause_level(num)

                # 关闭比当前层级深或相同的栈节点
                for k in [k for k in stack if k >= lvl]:
                    flush_clause(stack.pop(k))

                new_clause: dict[str, Any] = {
                    "standard_id": standard_id,
                    "clause_path": num,
                    "level": lvl,
                    "title": text,
                    "content": "",
                    "tables": [],
                    "images": [],
                    "page": page_idx + 1,
                    # 以下字段在 flush 时填充
                    "is_mandatory": False,
                    "references_to": [],
                    "applicable_scope": {},
                }
                stack[lvl] = new_clause

            elif app_m:
                # 附录单独作为顶层条款
                for k in list(stack):
                    flush_clause(stack.pop(k))
                letter = app_m.group(1)
                new_clause = {
                    "standard_id": standard_id,
                    "clause_path": f"附录{letter}",
                    "level": 1,
                    "title": text,
                    "content": "",
                    "tables": [],
                    "images": [],
                    "page": page_idx + 1,
                    "is_mandatory": False,
                    "references_to": [],
                    "applicable_scope": {},
                }
                stack[1] = new_clause

            else:
                # 普通内容，挂到当前最深条款
                cur = current_clause()
                if cur is None:
                    continue  # 封面/前言等，跳过

                if elem.get("type") == "table":
                    table_data = {
                        "caption": text,
                        "body": extract_table_body(elem),
                        "page": page_idx + 1,
                    }
                    cur["tables"].append(table_data)
                elif elem.get("type") == "image":
                    img_path = (
                        elem.get("content", {})
                        .get("image_source", {})
                        .get("path", "")
                    )
                    cur["images"].append({"path": img_path, "caption": text, "page": page_idx + 1})
                else:
                    # 段落文本追加到 content
                    if cur["content"]:
                        cur["content"] += "\n" + text
                    else:
                        cur["content"] = text

    # flush 剩余栈
    for clause in stack.values():
        flush_clause(clause)

    return clauses


# ---------------------------------------------------------------------------
# 统计报告
# ---------------------------------------------------------------------------

def print_stats(clauses: list[dict]) -> None:
    total = len(clauses)
    mandatory = sum(1 for c in clauses if c["is_mandatory"])
    with_tables = sum(1 for c in clauses if c["tables"])
    with_refs = sum(1 for c in clauses if c["references_to"])

    by_level: dict[int, int] = {}
    for c in clauses:
        lvl = c["level"]
        by_level[lvl] = by_level.get(lvl, 0) + 1

    t = Table(title="条款树统计")
    t.add_column("指标")
    t.add_column("数量", justify="right")
    t.add_row("总条款数", str(total))
    t.add_row("强制性条款（必须/严禁/不应/不得）", str(mandatory))
    t.add_row("含表格的条款", str(with_tables))
    t.add_row("含交叉引用的条款", str(with_refs))
    for lvl in sorted(by_level):
        t.add_row(f"  第 {lvl} 层条款", str(by_level[lvl]))
    console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--input",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="MinerU 输出的 _content_list_v2.json 路径。",
)
@click.option(
    "--standard-id",
    default="",
    help="规范标识，如 'GB 50016-2014(2018)'。留空则从文件名推断。",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
    help="输出目录，默认 data/structured/。",
)
@click.option(
    "--preview",
    is_flag=True,
    help="只打印前 20 条条款，不写文件（用于快速验证）。",
)
def main(
    input_path: Path,
    standard_id: str,
    output_dir: Path,
    preview: bool,
) -> None:
    """从 MinerU content_list_v2.json 构建结构化条款库。"""

    if not standard_id:
        # 从文件名推断：去掉 _content_list_v2.json 后缀
        standard_id = input_path.name.replace("_content_list_v2.json", "").replace("_", " ").strip()

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]输入：[/bold]{input_path}")

    with open(input_path, encoding="utf-8") as f:
        pages = json.load(f)

    console.print(f"共 {len(pages)} 页，开始解析…")
    clauses = parse_content_list(pages, standard_id)
    console.print(f"[green]✓ 提取到 {len(clauses)} 条条款[/green]")

    print_stats(clauses)

    if preview:
        console.print("\n[bold]--- 前 20 条预览 ---[/bold]")
        for c in clauses[:20]:
            mandatory_tag = " [red][强条][/red]" if c["is_mandatory"] else ""
            refs = f"  引用→{c['references_to']}" if c["references_to"] else ""
            console.print(
                f"  [cyan]{c['clause_path']}[/cyan] "
                f"(p{c['page']}) {c['title'][:60]}"
                f"{mandatory_tag}{refs}"
            )
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = standard_id.replace(" ", "_").replace("(", "").replace(")", "")
    out_path = output_dir / f"{safe_name}_clauses.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clauses, f, ensure_ascii=False, indent=2)

    console.print(f"[green]✓ 已写入 {out_path}[/green]")


if __name__ == "__main__":
    main()
