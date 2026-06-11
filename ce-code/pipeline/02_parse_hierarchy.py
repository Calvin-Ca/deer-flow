"""三轴层级化解析流水线 — 阶段 1（结构轴）到阶段 3（语义轴）。

PRD §3.2 三轴模型：
  阶段 1 结构轴  按文档原生目录建树（node_type + 层级栈，深度自适应）
  阶段 2 粒度轴  切最细自然单元（当前 stub：node 粒度；chunk_granularity 配置预留）
  阶段 3 语义轴  强条 / 引用 / 祖先链富化（调 extract/build.enrich）

terminal_stage 控制在哪层停止，供对比实验。阶段 0（MinerU）最贵，
只跑一次后缓存于 data/parsed/，本脚本从阶段 1 起读缓存。

输入：data/parsed/<standard>/<mode>/<standard>_content_list.json
输出：data/structured/<standard>/<profile>/
        structure.json  —— 阶段 1 产物（结构树，扁平化）
        chunks.json     —— 阶段 2 产物（粒度化 chunk 列表）
        clauses.json    —— 阶段 3 产物（v2 富化 + v1 兼容桥，供 04 建索引）
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

import click
from rich.console import Console
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent  # ce-code/
DEFAULT_OUTPUT = ROOT / "data" / "structured"

# extract/build 需要从 ROOT import，提前加进 path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 格式适配 — adapt_mineru_v1（纯格式适配，无结构语义）
# ---------------------------------------------------------------------------


class _HTMLTableParser(HTMLParser):
    """把单个 ``<table>`` HTML 解析成「行→(文本, colspan, rowspan)」原始单元格列表。

    参数：
        无（构造后调用 feed(html)，结果从 raw_rows 取）。
    返回：
        无（数据存于 self.raw_rows）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.raw_rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._buf: list[str] | None = None
        self._colspan = 1
        self._rowspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """开标签：``<tr>`` 起新行，``<td>/<th>`` 起新单元格并读出 colspan/rowspan。"""
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            a = dict(attrs)
            self._buf = []
            self._colspan = int(a.get("colspan") or 1)
            self._rowspan = int(a.get("rowspan") or 1)

    def handle_data(self, data: str) -> None:
        """单元格内文本：累积进当前缓冲。"""
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        """闭标签：``</td>`` 收单元格，``</tr>`` 收整行。"""
        if tag in ("td", "th") and self._buf is not None and self._row is not None:
            self._row.append(("".join(self._buf).strip(), self._colspan, self._rowspan))
            self._buf = None
        elif tag == "tr" and self._row is not None:
            self.raw_rows.append(self._row)
            self._row = None


def _expand_spans(raw_rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """带 colspan/rowspan 的原始单元格 → 矩形二维表（列对齐，防串列）。

    参数：
        raw_rows (list): _HTMLTableParser.raw_rows，每行为 (text, colspan, rowspan) 列表。
    返回：
        list[list[str]]: 展开后的矩形网格，行列严格对齐。
    """
    grid: list[list[str]] = []
    carry: dict[int, list] = {}
    for raw in raw_rows:
        row: list[str] = []
        col = 0
        ci = 0
        while ci < len(raw) or any(k >= col for k in carry):
            if col in carry:
                remaining, val = carry[col]
                row.append(val)
                if remaining - 1 > 0:
                    carry[col] = [remaining - 1, val]
                else:
                    del carry[col]
                col += 1
                continue
            if ci >= len(raw):
                break
            text, cspan, rspan = raw[ci]
            ci += 1
            for k in range(cspan):
                val = text if k == 0 else ""
                row.append(val)
                if rspan > 1:
                    carry[col] = [rspan - 1, val]
                col += 1
        grid.append(row)
    return grid


def _html_table_to_rows(html: str) -> list[list[str]]:
    """HTML ``<table>`` 串 → 矩形二维表体；空串返回空表。

    参数：
        html (str): HTML 表格字符串。
    返回：
        list[list[str]]: 二维表格内容。
    """
    if not html:
        return []
    parser = _HTMLTableParser()
    parser.feed(html)
    return _expand_spans(parser.raw_rows)


def adapt_mineru_v1(items: list[dict]) -> list[dict]:
    """MinerU v1（扁平 list）原始 JSON → 最小元素列表（纯格式适配）。

    只做三件事：① page_number 跳过（噪声）；② page 归一（0-base → 1-base）；
    ③ 表格 HTML 解析（table_body → 矩形二维表体）。
    结构语义判断（is_heading / TOC 过滤 / list 拆分）由结构轴 HierarchyBuilder._normalize_elements 处理。

    参数：
        items (list[dict]): MinerU v1 输出的扁平元素 list。
    返回：
        list[dict]: 最小元素列表，schema 为：
            type      元素类型（text / list / table / equation / footer）
            page      页码，从 1 起
            raw       原始 v1 dict
            caption   表格标题文本（仅 table）
            body      矩形二维表体（仅 table）
            img_path  裁切图路径（仅 table）
    """
    out: list[dict] = []
    for it in items:
        t = it.get("type", "text")
        page = it.get("page_idx", 0) + 1

        if t == "page_number":
            continue

        if t == "table":
            parts = it.get("table_caption") or []
            caption = " ".join(s.strip() for s in parts if isinstance(s, str) and s.strip())
            out.append({
                "type": "table",
                "page": page,
                "raw": it,
                "caption": caption,
                "body": _html_table_to_rows(it.get("table_body", "")),
                "img_path": it.get("img_path", ""),
            })
            continue

        out.append({"type": t, "page": page, "raw": it})
    return out


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
# 正则模式（结构轴）
# ---------------------------------------------------------------------------

CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[　 一-鿿]")
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")
APPENDIX_CLAUSE_RE = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,2})\s*[一-鿿]")
_TOC_TAIL_RE = re.compile(r"[（(]\s*\d+\s*[）)]\s*$")

# ---------------------------------------------------------------------------
# node_type 推断（结构轴核心分类，对应 PRD §3.1 节点元数据 schema）
# ---------------------------------------------------------------------------

_APPENDIX_ROOT_RE = re.compile(r"^附录\s*[A-Z]$")


def _infer_node_type(path: str, level: int) -> str:
    """根据条款路径和层级推断 node_type。

    参数：
        path (str): 条款号，如 "1" / "5.3.4" / "附录E" / "E.1.1"。
        level (int): 层级（小数点段数，附录按字母前缀计）。
    返回：
        str: node_type 枚举值 — chapter / section / clause / appendix。
            paragraph / table / formula / figure 由各自挂载点在 _attach 内按元素类型赋值。
    """
    if _APPENDIX_ROOT_RE.match(path):
        return "appendix"
    if level == 1:
        return "chapter"
    if level == 2:
        return "section"
    return "clause"


# ---------------------------------------------------------------------------
# 结构轴 — HierarchyBuilder（阶段 1）
# ---------------------------------------------------------------------------


def _is_toc_list(items: list[str], thresh: float = 0.5) -> bool:
    """目录列表判定：过半条目以「(页码)」结尾则整列为目录，应丢弃。

    参数：
        items (list[str]): MinerU list 元素的 list_items。
        thresh (float): 命中比例阈值，默认 0.5。
    返回：
        bool: True = 目录列表。
    """
    if not items:
        return False
    hits = sum(1 for x in items if _TOC_TAIL_RE.search(x.strip()))
    return hits >= max(1, len(items)) * thresh


class HierarchyBuilder:
    """把规范化元素列表建成扁平条款树（阶段 1 结构轴实现）。

    功能：
        按文档原生目录顺序逐元素建树，输出含 node_type / ancestor_titles / level
        的扁平节点列表，供阶段 2（粒度轴）和阶段 3（语义轴）消费。

    参数：
        standard_id (str): 规范唯一标识，写入每条节点的 standard_id 字段。
    返回：
        调用 build(elements) 返回 list[dict]。
    """

    def __init__(self, standard_id: str) -> None:
        """初始化建树器。

        参数：
            standard_id (str): 规范唯一标识（如输入文件 basename）。
        返回：
            无。
        """
        self.standard_id = standard_id
        self.clauses: list[dict] = []
        self.stack: dict[int, dict] = {}

    @staticmethod
    def _normalize_elements(raw_elements: list[dict]) -> list[dict]:
        """结构轴元素规范化：is_heading 标记 + list TOC 过滤 + list 条目拆分。

        adapt_mineru_v1 只做格式适配，结构语义判断集中在此：
          - is_heading：raw dict 含 text_level 字段即为标题（MinerU 原生标志）
          - list TOC 过滤：整列过半条目以页码结尾则丢弃（含附录中/英文目录）
          - list 条目拆分：每个 list_items 子条目单独 emit，使 1.0.1~1.0.7 各自可被识别为条款
        产出统一元素 schema 供 _consume 消费：{type, text, page, is_heading, raw, body?, img_path?}

        参数：
            raw_elements (list[dict]): adapt_mineru_v1 产出的最小元素列表。
        返回：
            list[dict]: 统一规范化元素列表。
        """
        out: list[dict] = []
        for elem in raw_elements:
            t = elem["type"]
            raw = elem["raw"]
            page = elem["page"]

            if t == "table":
                out.append({
                    "type": "table",
                    "text": elem.get("caption", ""),
                    "page": page,
                    "is_heading": False,
                    "raw": raw,
                    "body": elem.get("body", []),
                    "img_path": elem.get("img_path", ""),
                })
                continue

            if t == "list":
                li = raw.get("list_items", [])
                if _is_toc_list(li):
                    continue
                for sub in li:
                    sub = sub.strip()
                    if sub:
                        out.append({"type": "list", "text": sub, "page": page,
                                    "is_heading": False, "raw": raw})
                continue

            # text / equation / footer
            text = raw.get("text", "").strip()
            if not text:
                continue
            out.append({
                "type": t,
                "text": text,
                "page": page,
                "is_heading": "text_level" in raw,
                "raw": raw,
            })
        return out

    def build(self, raw_elements: list[dict]) -> list[dict]:
        """建树主入口：元素规范化 → 逐元素建树 → flush 余栈 → 排序。

        参数：
            raw_elements (list[dict]): adapt_mineru_v1 产出的最小元素列表。
        返回：
            list[dict]: 扁平节点列表（已按条款号排序）。强制性 / 交叉引用等语义字段由阶段 3 语义轴填充。
        """
        for elem in self._normalize_elements(raw_elements):
            self._consume(elem)
        for clause in self.stack.values():
            self._flush(clause)
        self._sort()
        return self.clauses

    # ---- 纯函数辅助 --------------------------------------------------------

    @staticmethod
    def _clause_level(num_str: str) -> int:
        """条款层级 = 条号的小数点段数。

        参数：
            num_str (str): 条款号，如 "5.3.4"。
        返回：
            int: 层级，"5.3.4" → 3。
        """
        return len(num_str.split("."))

    @staticmethod
    def _clause_match(text: str, is_heading: bool) -> re.Match | None:
        """识别行首正文数字条款编号。

        参数：
            text (str): 待匹配文本。
            is_heading (bool): 是否被标记为标题。
        返回：
            re.Match | None: 命中返回 match（group(1) 为条号），否则 None。
        """
        m = CLAUSE_NUM_RE.match(text)
        if m is None:
            return None
        num = m.group(1)
        if "." not in num and not is_heading:
            return None
        return m

    def _make_clause(self, path: str, level: int, content: str, page: int,
                     title: str | None = None, node_type: str | None = None) -> dict:
        """新建一个节点（正文 / 附录共用）。

        参数：
            path (str): 条款号 / clause_path，如 "5.3.4" / "附录E"。
            level (int): 层级。
            content (str): 节点正文（后续可被 _attach 追加）。
            page (int): 节点首次出现的页码（从 1 起）。
            title (str | None): 标题；缺省则用 path。
            node_type (str | None): 显式指定类型；缺省则由 _infer_node_type 推断。
        返回：
            dict: 节点 dict，含 standard_id / node_type / clause_path / level /
                  title / content / tables / images / page。
                  强制性 / 引用 / 祖先链等语义字段由阶段 3 语义轴补充。
        """
        return {
            "standard_id": self.standard_id,
            "node_type": node_type or _infer_node_type(path, level),
            "clause_path": path,
            "level": level,
            "title": title or path,
            "content": content,
            "tables": [],
            "images": [],
            "page": page,
        }

    # ---- 状态操作 ----------------------------------------------------------

    def _current(self) -> dict | None:
        """取当前节点（层级栈顶）。

        参数：无。
        返回：dict | None: 栈顶节点；空栈返回 None。
        """
        return self.stack[max(self.stack)] if self.stack else None

    def _flush(self, clause: dict) -> None:
        """节点定稿落库（空节点丢弃）。

        参数：
            clause (dict): 待落库的节点。
        返回：
            无（就地 append 到 self.clauses）。
        """
        text = clause.get("content", "").strip()
        if not text and not clause.get("tables"):
            return
        self.clauses.append(clause)

    # ---- 单元素分派 --------------------------------------------------------

    def _consume(self, elem: dict) -> None:
        """处理单个规范化元素（建树主循环的一步）。

        参数：
            elem (dict): 一个规范化元素 {type, text, page, is_heading, ...}。
        返回：
            无（就地改 self.stack / self.clauses）。
        """
        text = elem["text"]
        is_heading = elem.get("is_heading", False)

        if elem["type"] in ("text", "list") and len(text) < 60 and _TOC_TAIL_RE.search(text):
            return

        m = self._clause_match(text, is_heading)
        app_m = APPENDIX_RE.match(text)
        appc_m = APPENDIX_CLAUSE_RE.match(text)

        if m or appc_m:
            self._open_clause(text, m, appc_m, elem)
        elif app_m:
            self._open_appendix_root(app_m, text, elem)
        else:
            self._attach(elem, text)

    def _open_clause(self, text: str, m: re.Match | None, appc_m: re.Match | None,
                     elem: dict) -> None:
        """开一条新节点（正文数字条号或附录字母条号）。

        参数：
            text (str): 当前元素文本。
            m (re.Match | None): 正文条号 match。
            appc_m (re.Match | None): 附录字母条号 match。
            elem (dict): 当前规范化元素。
        返回：
            无（就地改 self.stack / self.clauses）。
        """
        path = m.group(1) if m else appc_m.group(1)
        if text[len(path):len(path) + 1] in "节条款项":
            return

        lvl = self._clause_level(path)
        body = text[len(path):].strip()

        for k in [k for k in self.stack if k >= lvl]:
            self._flush(self.stack.pop(k))
        self.stack[lvl] = self._make_clause(path, lvl, body, elem["page"])

    def _open_appendix_root(self, app_m: re.Match, text: str, elem: dict) -> None:
        """开一个附录根节点（附录A / 附录B 等）。

        参数：
            app_m (re.Match): 附录根 match（group(1) 为字母）。
            text (str): 整行标题文本。
            elem (dict): 当前规范化元素。
        返回：
            无（就地改 self.stack / self.clauses）。
        """
        for k in list(self.stack):
            self._flush(self.stack.pop(k))
        self.stack[1] = self._make_clause(
            f"附录{app_m.group(1)}", 1, "", elem["page"],
            title=text, node_type="appendix",
        )

    def _attach(self, elem: dict, text: str) -> None:
        """把非条号元素挂到当前节点。

        参数：
            elem (dict): 当前规范化元素。
            text (str): 元素文本（表格/图片为 caption，文字为正文）。
        返回：
            无（就地改栈顶节点）。
        """
        cur = self._current()
        if cur is None:
            return

        if elem["type"] == "table":
            cur["tables"].append({
                "caption": text,
                "body": elem.get("body", []),
                "page": elem["page"],
            })
        elif elem["type"] == "image":
            cur["images"].append({
                "path": elem.get("img_path", ""),
                "caption": text,
                "page": elem["page"],
            })
        else:
            cur["content"] = (cur["content"] + "\n" + text).lstrip("\n")

    # ---- 收尾 --------------------------------------------------------------

    @staticmethod
    def _sort_key(c: dict) -> tuple:
        """排序键：正文(数字号) < 附录(字母号) < 异常。

        参数：
            c (dict): 节点（读 clause_path）。
        返回：
            tuple: 5 元组排序键。
        """
        path = c["clause_path"]
        root = re.match(r"^附录\s*([A-Z])$", path)
        if root:
            return (1, ord(root.group(1)), 0, 0, 0)
        ap = re.match(r"^([A-Z])\.(\d+(?:\.\d+){0,2})$", path)
        if ap:
            nums = [int(x) for x in ap.group(2).split(".")]
            return (1, ord(ap.group(1))) + tuple(nums) + (0,) * (3 - len(nums))
        try:
            parts = [int(x) for x in path.split(".")]
            return (0,) + tuple(parts) + (0,) * (4 - len(parts))
        except ValueError:
            return (2, 0, 0, 0, 0)

    def _sort(self) -> None:
        """按条款编号重排 self.clauses。

        参数：无。
        返回：无（就地排序）。
        """
        self.clauses.sort(key=self._sort_key)

    def print_stats(self) -> None:
        """打印节点树统计报告（需在 build() 之后调用）。

        参数：无。
        返回：无（打印到终端）。
        """
        clauses = self.clauses
        total = len(clauses)
        with_tables = sum(1 for c in clauses if c["tables"])
        by_level: dict[int, int] = {}
        by_type: dict[str, int] = {}
        for c in clauses:
            by_level[c["level"]] = by_level.get(c["level"], 0) + 1
            nt = c.get("node_type", "?")
            by_type[nt] = by_type.get(nt, 0) + 1

        t = Table(title="节点树统计（阶段 1 结构轴）")
        t.add_column("指标")
        t.add_column("数量", justify="right")
        t.add_row("总节点数", str(total))
        t.add_row("含表格的节点", str(with_tables))
        for lvl in sorted(by_level):
            t.add_row(f"  层级 {lvl}", str(by_level[lvl]))
        for nt in sorted(by_type):
            t.add_row(f"  node_type={nt}", str(by_type[nt]))
        console.print(t)


# ---------------------------------------------------------------------------
# 粒度轴 — 阶段 2（当前 stub）
# ---------------------------------------------------------------------------


def _apply_granularity(nodes: list[dict], profile: ParseProfile) -> list[dict]:
    """粒度轴（阶段 2）：把结构节点切成最细 chunk 单元。

    功能：
        chunk_granularity=node（当前唯一实现）：原样返回，每节点即一个 chunk。
        paragraph / natural 待实现（TODO ② 粒度轴）——节点 content 按自然段拆分，
        拆后每个 chunk 保留父节点的 clause_path / ancestor_titles 供 small-to-big 回补。

    参数：
        nodes (list[dict]): 阶段 1 产出的结构节点列表。
        profile (ParseProfile): 解析配置（读 chunk_granularity）。
    返回：
        list[dict]: chunk 列表（当前与 nodes 相同）。
    """
    if profile.chunk_granularity != "node":
        console.print(
            f"[yellow]⚠ chunk_granularity={profile.chunk_granularity!r} 暂未实现，"
            f"退回 node 粒度[/yellow]"
        )
    return nodes


# ---------------------------------------------------------------------------
# 语义轴 — 阶段 3（调 extract/build.enrich）
# ---------------------------------------------------------------------------


def _apply_enrichment(
    chunks: list[dict],
    profile: ParseProfile,
    *,
    official: set[str] | None = None,
    version: str = "",
    effective_date: str = "",
    status: str = "active",
) -> list[dict]:
    """语义轴（阶段 3）：强条 / 引用 / 祖先链富化。

    功能：
        enrichment=none  → 原样返回（跳过富化，供消融实验）；
        ids_refs / full  → 调 extract/build.enrich 跑完整富化链（当前两者等价，
                           full 后续会加 scope 谓词抽取）。

    参数：
        chunks (list[dict]): 阶段 2 产出的 chunk 列表。
        profile (ParseProfile): 解析配置（读 enrichment）。
        official (set[str] | None): 官方强条条款号集合；None → 保守模式。
        version (str): 规范版本号，如 "2018"。
        effective_date (str): 生效日期，如 "2018-10-01"。
        status (str): 条款状态，active / superseded / abolished。
    返回：
        list[dict]: v2 富化条款列表（含 v1 兼容桥字段）。
    """
    if profile.enrichment == "none":
        console.print("[yellow]enrichment=none，跳过语义轴[/yellow]")
        return chunks

    from extract import build as _build  # 延迟 import，避免无 GPU 环境加载失败

    enriched = _build.enrich(
        chunks,
        official=official,
        version=version,
        effective_date=effective_date,
        status=status,
    )
    return enriched


# ---------------------------------------------------------------------------
# 流水线入口
# ---------------------------------------------------------------------------


def run_pipeline(
    elements: list[dict],
    standard_id: str,
    profile: ParseProfile,
    out_root: Path,
    *,
    official: set[str] | None = None,
    version: str = "",
    effective_date: str = "",
    status: str = "active",
) -> list[dict]:
    """按 profile.terminal_stage 跑阶段 1-3，产物写入 out_root/{standard}/{profile.name}/。

    功能：
        阶段 1 始终跑（建树）；根据 terminal_stage 决定是否继续到阶段 2（粒度）和
        阶段 3（增强）。每阶段在产物目录写一个 JSON 文件，阶段 4（索引）由
        04_build_index.py 负责。

    参数：
        elements (list[dict]): adapt_mineru_v1 产出的最小元素列表（格式适配后，结构语义由 HierarchyBuilder 处理）。
        standard_id (str): 规范唯一标识（目录名的一部分）。
        profile (ParseProfile): 解析配置。
        out_root (Path): 产物根目录（data/structured/）。
        official (set[str] | None): 官方强条集合，传给语义轴。
        version / effective_date / status: 规范元数据，传给语义轴。
    返回：
        list[dict]: 最终阶段的产物节点列表。
    """
    # 产物目录：data/structured/{standard_id}/{profile.name}/
    safe_std = re.sub(r"[^\w\-]", "_", standard_id)
    out_dir = out_root / safe_std / profile.name
    out_dir.mkdir(parents=True, exist_ok=True)

    # 阶段 1：结构轴
    console.print(f"[bold cyan]阶段 1 结构轴[/bold cyan] → {out_dir / 'structure.json'}")
    builder = HierarchyBuilder(standard_id)
    nodes = builder.build(elements)
    _write_json(nodes, out_dir / "structure.json")
    builder.print_stats()

    if profile.terminal_stage == "structure":
        return nodes

    # 阶段 2：粒度轴
    console.print(f"[bold cyan]阶段 2 粒度轴[/bold cyan]  chunk_granularity={profile.chunk_granularity!r}")
    chunks = _apply_granularity(nodes, profile)
    _write_json(chunks, out_dir / "chunks.json")

    if profile.terminal_stage == "granularity":
        return chunks

    # 阶段 3：语义轴
    console.print(f"[bold cyan]阶段 3 语义轴[/bold cyan]  enrichment={profile.enrichment!r}")
    enriched = _apply_enrichment(
        chunks, profile,
        official=official,
        version=version,
        effective_date=effective_date,
        status=status,
    )
    _write_json(enriched, out_dir / "clauses.json")

    # terminal_stage == "index" 时 04_build_index.py 接手，本脚本不处理
    if profile.terminal_stage == "index":
        console.print("[yellow]terminal_stage=index：请手动跑 04_build_index.py 指向上述 clauses.json[/yellow]")

    return enriched


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
@click.option("--official", "official_path", type=click.Path(path_type=Path), default=None,
              help="官方强条清单 JSON（[\"3.1.2\",...]）；不传则保守模式。")
@click.option("--version", default="", help="规范版本，如 2018。")
@click.option("--effective-date", default="", help="生效日期，如 2018-10-01。")
@click.option("--status", default="active", help="active / superseded / abolished。")
@click.option("--preview", is_flag=True, help="只打印阶段 1 前 20 条节点，不写文件。")
def main(
    input_path: Path,
    standard_id: str,
    output_dir: Path,
    profile_name: str,
    terminal_stage: str,
    chunk_granularity: str,
    enrichment: str,
    official_path: Path | None,
    version: str,
    effective_date: str,
    status: str,
    preview: bool,
) -> None:
    """三轴层级化解析：从 MinerU content_list.json 构建知识库（阶段 1-3）。

    产物路径：data/structured/<standard>/<profile>/
      structure.json  —— 阶段 1（结构轴）
      chunks.json     —— 阶段 2（粒度轴）
      clauses.json    —— 阶段 3（语义轴，供 04_build_index.py 建索引）
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

    elements = adapt_mineru_v1(data)
    console.print(f"共 {len(elements)} 个原始元素（page_number 已过滤），开始解析…")

    if preview:
        builder = HierarchyBuilder(standard_id)
        nodes = builder.build(elements)
        console.print(f"[green]✓ 提取到 {len(nodes)} 个节点[/green]")
        builder.print_stats()
        console.print("\n[bold]--- 前 20 条节点预览 ---[/bold]")
        for c in nodes[:20]:
            tables = f"  [{len(c['tables'])}表]" if c.get("tables") else ""
            console.print(
                f"  [cyan]{c['clause_path']}[/cyan] [{c.get('node_type', '?')}]"
                f" (p{c['page']}) {c['title'][:50]}{tables}"
            )
        return

    official: set[str] | None = None
    if official_path:
        from extract import strength as _strength
        official = _strength.load_official(official_path)

    run_pipeline(
        elements, standard_id, profile, output_dir,
        official=official,
        version=version,
        effective_date=effective_date,
        status=status,
    )


if __name__ == "__main__":
    main()
