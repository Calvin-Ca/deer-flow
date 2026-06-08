"""阶段 0 第二步：从 MinerU 的 content_list.json 构建条款树。

支持两种 MinerU 输出格式：
  v1 (content_list.json)：扁平列表，每条有 text / page_idx / text_level 字段
  v2 (content_list_v2.json)：外层按页嵌套，文本藏在 paragraph_content / title_content 中

实践中优先用 v1——顺序更可靠，文本字段更直接。

输入：data/parsed/<standard>/<mode>/<standard>_content_list.json
输出：data/structured/<standard>_clauses.json

每条款格式见 ce-code/PRD.md §1。
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
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
# 格式检测
# ---------------------------------------------------------------------------

def detect_format(data: Any) -> str:
    """自动检测 MinerU content_list 格式版本（v1 扁平 list / v2 外层按页嵌套）。"""
    if not data:
        return "v1"
    return "v2" if isinstance(data[0], list) else "v1"


# ---------------------------------------------------------------------------
# 共享表格工具：HTML <table> → 矩形二维表
#   v1(table_body) 与 v2(content.html) 的表体都是 HTML 串，解析算法与格式无关，
#   故抽成共享 helper；两个 reader 只负责「从各自字段取出 HTML」。
# ---------------------------------------------------------------------------

class _HTMLTableParser(HTMLParser):
    """把单个 ``<table>`` HTML 解析成「行→(文本, colspan, rowspan)」原始单元格列表。

    只收集结构，不在此展开 span（展开交给 _expand_spans，便于单测两段逻辑）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.raw_rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._buf: list[str] | None = None
        self._colspan = 1
        self._rowspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            a = dict(attrs)
            self._buf = []
            self._colspan = int(a.get("colspan") or 1)
            self._rowspan = int(a.get("rowspan") or 1)

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._buf is not None and self._row is not None:
            self._row.append(("".join(self._buf).strip(), self._colspan, self._rowspan))
            self._buf = None
        elif tag == "tr" and self._row is not None:
            self.raw_rows.append(self._row)
            self._row = None


def _expand_spans(raw_rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """把带 colspan/rowspan 的原始单元格展开成**矩形**二维表（列对齐，防止串列）。

    约定（保真优先，不臆造数据）：
    - colspan=N：文本只放第 1 列，其余 N-1 列补空串；
    - rowspan=M：把该列的值在随后 M-1 行的**同一列**继续占位（值可能是空串）。
    这样产出的网格行列严格对齐，下游「给定行列取值」才不会错位。表头合并单元格是否
    前向填充（forward-fill）属语义决策，留给构建层 extract/tables.py（波2）按需处理。
    """
    grid: list[list[str]] = []
    carry: dict[int, list] = {}  # 列号 -> [剩余行数, 值]，记录 rowspan 跨行占位
    for raw in raw_rows:
        row: list[str] = []
        col = 0
        ci = 0
        # 当前行还有待放的原始单元格，或仍有 rowspan 占位需要落到本行(>= 当前列)时继续
        while ci < len(raw) or any(k >= col for k in carry):
            if col in carry:  # 该列被上方 rowspan 占住，先填占位值
                remaining, val = carry[col]
                row.append(val)
                if remaining - 1 > 0:
                    carry[col] = [remaining - 1, val]
                else:
                    del carry[col]
                col += 1
                continue
            if ci >= len(raw):  # 本行原始单元格已用完，剩下的高列占位下轮再处理
                break
            text, cspan, rspan = raw[ci]
            ci += 1
            for k in range(cspan):
                val = text if k == 0 else ""  # colspan 只首列留值，其余补空
                row.append(val)
                if rspan > 1:
                    carry[col] = [rspan - 1, val]  # rowspan 跨行占位
                col += 1
        grid.append(row)
    return grid


def _html_table_to_rows(html: str) -> list[list[str]]:
    """HTML ``<table>`` 串 → 矩形二维表体；空串返回空表。"""
    if not html:
        return []
    parser = _HTMLTableParser()
    parser.feed(html)
    return _expand_spans(parser.raw_rows)


def _v2_cells_to_rows(table_body: list) -> list[list[str]]:
    """v2 旧子版本：``content.table_body`` 的 cell 结构（非 HTML）→ 二维表体。"""
    rows: list[list[str]] = []
    for row in table_body or []:
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
# 元素规范化：按 MinerU 格式分两个 reader，各自吃原始 JSON、吐**统一**规范化元素
#   统一 schema：{ type, text, page, is_heading, raw, body?, img_path? }
#     body     表体二维表（仅 table 元素，reader 内已按各自格式抽好）
#     img_path 图/表的裁切图路径（table / image 元素）
#   下游 parse_elements 只认这套 schema，对 v1/v2 无感——格式差异锁死在 reader 内。
# ---------------------------------------------------------------------------

def _join_text(parts: list) -> str:
    """caption/content 片段 → 单行文本。兼容 v1 的 list[str] 与 v2 的 list[{type,content}]。"""
    out = []
    for x in parts or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and x.get("type") == "text":
            out.append(x.get("content", ""))
    return " ".join(s for s in out if s).strip()


def read_v1(items: list[dict]) -> list[dict]:
    """MinerU **v1**（扁平 list）→ 统一规范化元素。字段均为顶层键、无 content 包裹：

      text        text(+可选 text_level=标题层级)
      list        list_items: list[str]（**无 text 字段**，需合并条目，否则整段丢失）
      table       table_body: **HTML 串** + table_caption: list[str] + img_path
      equation    text: LaTeX（$$..$$）
      footer      text（页脚/表注）
      page_number text（页码，**丢弃**：是噪声，且 "1/2" 易被误判成章节号）
    """
    out: list[dict] = []
    for it in items:
        t = it.get("type", "text")
        page = it.get("page_idx", 0) + 1

        if t == "page_number":
            continue

        if t == "table":
            out.append({
                "type": "table",
                "text": _join_text(it.get("table_caption")),
                "page": page,
                "is_heading": False,
                "raw": it,
                "body": _html_table_to_rows(it.get("table_body", "")),  # v1：table_body 是 HTML 串
                "img_path": it.get("img_path", ""),
            })
            continue

        if t == "list":
            text = "\n".join(it.get("list_items", []))  # list 无 text 字段，合并条目
        else:  # text / equation / footer
            text = it.get("text", "").strip()
        if not text:
            continue

        out.append({
            "type": t,
            "text": text,
            "page": page,
            "is_heading": "text_level" in it,  # 带 text_level 即标题（纯数字章节号靠它定位）
            "raw": it,
        })
    return out


def read_v2(pages: list[list[dict]]) -> list[dict]:
    """MinerU **v2**（外层按页嵌套）→ 统一规范化元素。文本/表/图都藏在 content 下：

      title/paragraph  content.title_content / paragraph_content: list[{type,content}]
      table            content.html: **HTML 串**（旧子版本是 content.table_body 的 cell 结构）
                       + content.table_caption + content.image_source.path（表格裁切图）
      image            content.image_caption + content.image_source.path
    """
    out: list[dict] = []
    for page_idx, page in enumerate(pages):
        page_no = page_idx + 1
        for it in page:
            t = it.get("type", "")
            c = it.get("content", {})

            if t == "table":
                out.append({
                    "type": "table",
                    "text": _join_text(c.get("table_caption")),
                    "page": page_no,
                    "is_heading": False,
                    "raw": it,
                    # 优先 html，回落旧 cell 结构（同为 v2 的两个子版本）
                    "body": _html_table_to_rows(c.get("html", "")) or _v2_cells_to_rows(c.get("table_body", [])),
                    "img_path": c.get("image_source", {}).get("path", ""),
                })
                continue

            if t == "image":
                out.append({
                    "type": "image",
                    "text": _join_text(c.get("image_caption")),
                    "page": page_no,
                    "is_heading": False,
                    "raw": it,
                    "img_path": c.get("image_source", {}).get("path", ""),
                })
                continue

            if t == "title":
                text, is_heading = _join_text(c.get("title_content")), True
            elif t == "paragraph":
                text, is_heading = _join_text(c.get("paragraph_content")), False
            else:
                continue
            if not text:
                continue

            out.append({
                "type": t,
                "text": text,
                "page": page_no,
                "is_heading": is_heading,
                "raw": it,
            })
    return out


def normalize_elements(data: Any) -> list[dict]:
    """按格式分发到 read_v1 / read_v2，产出统一规范化元素列表。"""
    return read_v2(data) if detect_format(data) == "v2" else read_v1(data)


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

            if elem["type"] == "table":
                cur["tables"].append({
                    "caption": text,
                    "body": elem.get("body", []),       # 表体已在 reader 内按格式抽好
                    "page": elem["page"],
                })
            elif elem["type"] == "image":
                cur["images"].append({
                    "path": elem.get("img_path", ""),   # 图片路径已在 reader 内按格式抽好
                    "caption": text,
                    "page": elem["page"],
                })
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
