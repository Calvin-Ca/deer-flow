"""阶段 0 第二步：从 MinerU 的 content_list.json 构建条款树。

支持两种 MinerU 输出格式：
  v1 (content_list.json)：扁平列表，每条有 text / page_idx / text_level 字段
  v2 (content_list_v2.json)：外层按页嵌套，文本藏在 paragraph_content / title_content 中

实践中优先用 v1——顺序更可靠，文本字段更直接。

输入：data/parsed/<standard>/<mode>/<standard>_content_list.json
输出：data/structured/<standard>_clauses.json

每条款格式见 CLAUDE.md §4.1。
"""

from __future__ import annotations

import json
import re
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

# 条款编号：1 / 1.1 / 1.1.1 / 1.1.1.1（行首，后跟空白或中文字符）
CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[　 一-鿿]")

# 附录：附录A / 附录 B
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")

# 强制性：必须/严禁/不应/不得 是硬强条；"应"在 GB 规范中是 shall（强制）
# 负向前瞻排除常见非强制用法：不应、应急、应对、应用、应付、应变、应运、应答
MANDATORY_RE = re.compile(r"必须|严禁|不应|不得|禁止|(?<![不无非])应(?!急|对|用|付|变|运|答|届)")

# 交叉引用：本规范内条文号引用
REFERENCE_RE = re.compile(
    r"(?:符合|按|按照|执行|见|参见|参照)\s*"
    r"(?:本[规标]范?\s*)?"
    r"第?\s*(\d+\.\d+(?:\.\d+)*)\s*(?:条|款|节)?"
    r"(?:\s*(?:的规定|的要求|执行))?"
)

# ---------------------------------------------------------------------------
# 格式检测与元素规范化
# ---------------------------------------------------------------------------

def detect_format(data: Any) -> str:
    """自动检测 MinerU content_list 格式版本。"""
    if not data:
        return "v1"
    first = data[0]
    if isinstance(first, list):
        return "v2"
    return "v1"


def normalize_elements(data: Any) -> list[dict]:
    """
    把 v1 / v2 格式统一成扁平的规范化元素列表：
      { type, text, page, is_heading, raw }
    """
    fmt = detect_format(data)

    if fmt == "v1":
        return _normalize_v1(data)
    else:
        return _normalize_v2(data)


def _normalize_v1(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        text = item.get("text", "").strip()
        if not text:
            continue
        result.append({
            "type": item.get("type", "text"),
            "text": text,
            "page": item.get("page_idx", 0) + 1,
            "is_heading": "text_level" in item,
            "raw": item,
        })
    return result


def _normalize_v2(pages: list[list[dict]]) -> list[dict]:
    result = []
    for page_idx, page in enumerate(pages):
        for item in page:
            t = item.get("type", "")
            c = item.get("content", {})

            if t == "title":
                parts = c.get("title_content", [])
                text = " ".join(x.get("content", "") for x in parts if x.get("type") == "text").strip()
                is_heading = True
            elif t == "paragraph":
                parts = c.get("paragraph_content", [])
                text = " ".join(x.get("content", "") for x in parts if x.get("type") == "text").strip()
                is_heading = False
            elif t == "table":
                caps = c.get("table_caption", [])
                text = " ".join(x.get("content", "") for x in caps if x.get("type") == "text").strip()
                is_heading = False
            elif t == "image":
                caps = c.get("image_caption", [])
                text = " ".join(x.get("content", "") for x in caps if x.get("type") == "text").strip()
                is_heading = False
            else:
                continue

            if not text and t not in ("table",):
                continue

            result.append({
                "type": t,
                "text": text,
                "page": page_idx + 1,
                "is_heading": is_heading,
                "raw": item,
            })
    return result


# ---------------------------------------------------------------------------
# 表格提取（仅 v2 有结构化 body）
# ---------------------------------------------------------------------------

def extract_table_body(raw_elem: dict) -> list[list[str]]:
    c = raw_elem.get("content", {})
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
# 条款树构建
# ---------------------------------------------------------------------------

def clause_level(num_str: str) -> int:
    return len(num_str.split("."))


def is_mandatory(text: str) -> bool:
    return bool(MANDATORY_RE.search(text))


def extract_references(text: str) -> list[str]:
    return list(dict.fromkeys(REFERENCE_RE.findall(text)))


def _clause_match(text: str, is_heading: bool) -> re.Match | None:
    """
    识别条款编号。规则：
    - X.X / X.X.X / X.X.X.X（有小数点）：任何元素都可匹配
    - 纯数字（如 "1 总则"）：仅在明确标记为标题（is_heading=True）时匹配，
      避免把年份（2006年）、页码等误识别为章号
    """
    m = CLAUSE_NUM_RE.match(text)
    if m is None:
        return None
    num = m.group(1)
    if "." not in num and not is_heading:
        return None
    return m


def parse_elements(elements: list[dict], standard_id: str) -> list[dict]:
    """把规范化元素列表解析成扁平条款列表。"""
    clauses: list[dict] = []
    stack: dict[int, dict] = {}  # level → clause_dict

    def current_clause() -> dict | None:
        return stack[max(stack)] if stack else None

    def flush_clause(clause: dict) -> None:
        text = clause.get("content", "").strip()
        if not text and not clause.get("tables"):
            return
        clause["is_mandatory"] = is_mandatory(text)
        clause["references_to"] = extract_references(text)
        clauses.append(clause)

    for elem in elements:
        text = elem["text"]
        is_heading = elem.get("is_heading", False)

        m = _clause_match(text, is_heading)
        app_m = APPENDIX_RE.match(text)

        if m:
            num = m.group(1)
            lvl = clause_level(num)

            # 从完整文本中分离条款号和正文
            # "3.2.1绿色建筑..." → title="3.2.1", body="绿色建筑..."
            body = text[len(num):].strip()

            for k in [k for k in stack if k >= lvl]:
                flush_clause(stack.pop(k))

            stack[lvl] = {
                "standard_id": standard_id,
                "clause_path": num,
                "level": lvl,
                "title": num,
                "content": body,   # 条款号后的正文直接放入 content
                "tables": [],
                "images": [],
                "page": elem["page"],
                "is_mandatory": False,
                "references_to": [],
                "applicable_scope": {},
            }

        elif app_m:
            for k in list(stack):
                flush_clause(stack.pop(k))
            letter = app_m.group(1)
            stack[1] = {
                "standard_id": standard_id,
                "clause_path": f"附录{letter}",
                "level": 1,
                "title": text,
                "content": "",
                "tables": [],
                "images": [],
                "page": elem["page"],
                "is_mandatory": False,
                "references_to": [],
                "applicable_scope": {},
            }

        else:
            cur = current_clause()
            if cur is None:
                continue

            raw = elem.get("raw", {})
            if elem["type"] == "table":
                cur["tables"].append({
                    "caption": text,
                    "body": extract_table_body(raw),
                    "page": elem["page"],
                })
            elif elem["type"] == "image":
                img_path = raw.get("content", {}).get("image_source", {}).get("path", "")
                cur["images"].append({"path": img_path, "caption": text, "page": elem["page"]})
            else:
                cur["content"] = (cur["content"] + "\n" + text).lstrip("\n")

    for clause in stack.values():
        flush_clause(clause)

    # 按条款编号排序（MinerU 某些页内顺序可能颠倒）
    def _sort_key(c: dict) -> tuple:
        path = c["clause_path"]
        if path.startswith("附录"):
            return (99,) + (0,) * 4
        try:
            parts = [int(x) for x in path.split(".")]
            return tuple(parts) + (0,) * (4 - len(parts))
        except ValueError:
            return (98,) + (0,) * 4

    clauses.sort(key=_sort_key)

    # 术语章（第2章，clause_path 以 "2." 开头）：定义文字含"应"不算强条
    for c in clauses:
        if c["clause_path"].startswith("2."):
            c["is_mandatory"] = bool(
                re.search(r"必须|严禁|不应|不得|禁止", c.get("content", ""))
            )

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
        by_level[c["level"]] = by_level.get(c["level"], 0) + 1

    t = Table(title="条款树统计")
    t.add_column("指标")
    t.add_column("数量", justify="right")
    t.add_row("总条款数", str(total))
    t.add_row("强制性条款（应/必须/严禁/不应/不得）", str(mandatory))
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
    "--input", "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="MinerU 输出的 _content_list.json 或 _content_list_v2.json 路径。",
)
@click.option("--standard-id", default="", help="规范标识，如 'GB 50016-2014(2018)'。")
@click.option(
    "--output-dir", "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
)
@click.option("--preview", is_flag=True, help="只打印前 20 条，不写文件。")
def main(input_path: Path, standard_id: str, output_dir: Path, preview: bool) -> None:
    """从 MinerU content_list.json 构建结构化条款库（支持 v1/v2 格式自动识别）。"""

    if not standard_id:
        standard_id = (
            input_path.name
            .replace("_content_list_v2.json", "")
            .replace("_content_list.json", "")
            .replace("_", " ")
            .strip()
        )

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]输入：[/bold]{input_path}")

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    fmt = detect_format(data)
    console.print(f"格式：{fmt}")

    elements = normalize_elements(data)
    console.print(f"共 {len(elements)} 个元素，开始解析…")

    clauses = parse_elements(elements, standard_id)
    console.print(f"[green]✓ 提取到 {len(clauses)} 条条款[/green]")

    print_stats(clauses)

    if preview:
        console.print("\n[bold]--- 前 20 条预览 ---[/bold]")
        for c in clauses[:20]:
            tag = " [red][强条][/red]" if c["is_mandatory"] else ""
            refs = f"  → {c['references_to']}" if c["references_to"] else ""
            console.print(
                f"  [cyan]{c['clause_path']}[/cyan] (p{c['page']}) "
                f"{c['title'][:60]}{tag}{refs}"
            )
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    safe = standard_id.replace(" ", "_").replace("(", "").replace(")", "")
    out_path = output_dir / f"{safe}_clauses.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clauses, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 已写入 {out_path}[/green]")


if __name__ == "__main__":
    main()
