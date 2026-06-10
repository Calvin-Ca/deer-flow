"""阶段 0 第二步：从 MinerU 的 content_list.json 构建条款树。

只支持 MinerU **v1**（content_list.json）：扁平列表，每条有 text / page_idx /
text_level 字段。v1 顺序可靠、文本字段直接，是本项目唯一采用的解析输入；管线
（mineru_api.py）也只产出 v1，故不再保留 v2（按页嵌套）的读取分支。

输入：data/parsed/<standard>/<mode>/<standard>_content_list.json
输出：data/structured/<standard>_clauses.json

每条款格式见 ce-code/PRD.md §1。
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

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

# 附录根：附录A / 附录 B
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")

# 附录条款号：字母前缀，如 E.1 / E.1.1 / E.10.2。条号后必须紧跟**中文**（用 [一-鿿]，
# 不含空格）——否则英文目录行 "F.1 Work… (78)" 会借空格误匹配；表号 "E.2.2-1" 后是连字符也不匹配。
APPENDIX_CLAUSE_RE = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,2})\s*[一-鿿]")

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
# 表格工具：HTML <table> → 矩形二维表
#   v1 的 table_body 是 HTML 串，解析算法与正文无关，单独抽成 helper；
#   read_v1 只负责「从 table_body 取出 HTML」。
# ---------------------------------------------------------------------------

class _HTMLTableParser(HTMLParser):
    """把单个 ``<table>`` HTML 解析成「行→(文本, colspan, rowspan)」原始单元格列表。

    只收集结构，不在此展开 span（展开交给 _expand_spans，便于单测两段逻辑）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.raw_rows: list[list[tuple[str, int, int]]] = []  # 解析产物：每行的原始单元格
        self._row: list[tuple[str, int, int]] | None = None   # 当前正在收集的行
        self._buf: list[str] | None = None                    # 当前单元格的文本缓冲
        self._colspan = 1                                     # 当前单元格的 colspan
        self._rowspan = 1                                     # 当前单元格的 rowspan

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
        """单元格内文本：累积进当前缓冲（缓冲为 None 表示当前不在单元格里，忽略）。"""
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        """闭标签：``</td>`` 把 (文本, colspan, rowspan) 存入当前行，``</tr>`` 把整行收进 raw_rows。"""
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


# ---------------------------------------------------------------------------
# 元素规范化：read_v1 吃原始 v1 JSON、吐**统一**规范化元素
#   统一 schema：{ type, text, page, is_heading, raw, body?, img_path? }
#     body     表体二维表（仅 table 元素，read_v1 内已抽好）
#     img_path 图/表的裁切图路径（table / image 元素）
#   下游 ClauseTreeBuilder 只认这套 schema。
# ---------------------------------------------------------------------------

def _join_text(parts: list) -> str:
    """caption 片段（v1 的 list[str]）→ 单行文本。"""
    return " ".join(s.strip() for s in (parts or []) if isinstance(s, str) and s.strip())


# 目录条目尾巴：以「(页码)」结尾（含半/全角括号）。目录行形如 "3.1 一般规定 (5)"，
# 其条号会被误当真条款、并与正文重复。两道防线：
#   - list 级（_is_toc_list）：整列剔除（附录目录行很长、带点引导，候选级长度闸拦不住）；
#   - 候选级（ClauseTreeBuilder._consume 入口）：拦 text 来源的短目录行（章节级、中英文 TOC）。
_TOC_TAIL_RE = re.compile(r"[（(]\s*\d+\s*[）)]\s*$")


def _is_toc_list(items: list[str], thresh: float = 0.5) -> bool:
    """目录列表判定：过半条目以「(页码)」结尾。

    目录行形如 'F.1 工程计量申请(核准)表 …… (78)'，整列剔除；正文内容列表
    （如 1.0.1~1.0.7 句子以「。」结尾）命中率为 0，不会误删。
    """
    if not items:
        return False
    hits = sum(1 for x in items if _TOC_TAIL_RE.search(x.strip()))
    return hits >= max(1, len(items)) * thresh


def read_v1(items: list[dict]) -> list[dict]:
    """MinerU **v1**（扁平 list）原始 JSON → 下游统一规范化元素列表。

    参数：
        items (list[dict]): MinerU v1 输出的扁平元素 list。每个 dict 至少含 type、page_idx（从 0 起），以及对应类型的取数字段：
              text        text(+可选 text_level=标题层级)
              list        list_items: list[str]
              table       table_body: HTML 串 + table_caption: list[str] + img_path
              equation    text: LaTeX（$$..$$）
              footer      text（页脚/表注）
              page_number text（页码）

    返回：
        list[dict]: 统一规范化元素列表，每个元素字段为：
              type       元素类型（text / list / table / equation / footer）
              text       单行文本（table 取 caption；page_number 整条丢弃，不出现）
              page       页码，从 1 起（= page_idx + 1）
              is_heading 是否标题（原始带 text_level 即 True）
              raw        原始 v1 dict
              body       矩形二维表，**仅 table 元素有**
              img_path   图/表裁切图路径，**仅 table 元素有**
            该 schema 是下游 ClauseTreeBuilder 唯一认的输入格式。
    """
    out: list[dict] = []
    for it in items:
        t = it.get("type", "text")
        page = it.get("page_idx", 0) + 1 # 页码归一：每条 page = page_idx + 1（v1 的 page_idx 从 0 起 → 统一成从 1 起）

        if t == "page_number": # 根据特征判定的页码，并非实际pdf页码
            continue   # （页码行）整条跳过——它是噪声，且 "1/2" 这种容易被误判成章节号

        if t == "table":
            out.append({
                "type": "table",
                "text": _join_text(it.get("table_caption")),  #  ["表 3.0.1", "工程量清单"],   # → _join_text → "表 3.0.1 工程量清单"
                "page": page,
                "is_heading": False,
                "raw": it,
                "body": _html_table_to_rows(it.get("table_body", "")),  # v1：table_body 是 HTML 串
                "img_path": it.get("img_path", ""),
            })
            continue

        if t == "list":
            li = it.get("list_items", [])
            if _is_toc_list(li):
                continue  # 目录列表整列丢弃（含附录中/英文目录），避免条号污染条款树
            # 其余 list 把每个条目单独 emit：多条款列表(如 1.0.1~1.0.7)才能各自被识别成条款；
            # 真枚举项(如「1. 修订…」无小数点条号)不会被 _clause_match 命中，自然并入正文。
            for sub in li:
                sub = sub.strip()
                if sub:
                    out.append({"type": "list", "text": sub, "page": page, "is_heading": False, "raw": it})
            continue

        text = it.get("text", "").strip()  # text / equation / footer
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


# ---------------------------------------------------------------------------
# 条款树构建
# ---------------------------------------------------------------------------
#
# 富化函数（强制性 / 交叉引用）留在模块级：它们是 enrichment 而非建树本身，且
# Phase B 路线图计划外迁到 extract/strength.py、extract/references.py（波1），
# 届时只需替换 _flush 里的调用点，无需从类里再拆出来。

def is_mandatory(text: str) -> bool:
    """是否强制性条款：命中强条正则（必须/严禁/不应/不得/禁止/应…）即为 True。"""
    return bool(MANDATORY_RE.search(text))


def extract_references(text: str) -> list[str]:
    """抽取条文内的交叉引用条号（如"按第 5.3.1 条"→ ``5.3.1``），去重且保持出现顺序。"""
    return list(dict.fromkeys(REFERENCE_RE.findall(text)))


class ClauseTreeBuilder:
    """把规范化元素列表（``read_v1`` 的产物）建成扁平条款树。

    功能:
        建树是个**有状态**的流水线——按文档顺序逐个吃元素，靠一个「层级栈」把条款归位
        成树（再拍平成列表），并在落库时补算强制性、交叉引用，最后排序、修正术语章。

    实例属性:
        standard_id (str): 规范唯一标识，写入每条条款的 ``standard_id`` 字段。
        stack (dict[int, dict]): ``level → 正在累积的条款``。新条款到来时，把同级及
            更深的旧条款先弹出落库，再压入新条款，从而实现层级归位。
        clauses (list[dict]): 已定稿落库的条款列表，即 ``build()`` 的最终产物。

    用法:
        ``ClauseTreeBuilder(standard_id).build(elements)``
    """

    def __init__(self, standard_id: str) -> None:
        """初始化建树器。

        参数:
            standard_id (str): 规范唯一标识（如输入文件 basename），写入每条条款。
        返回:
            无。
        """
        self.standard_id = standard_id
        self.clauses: list[dict] = []      # 已 flush 落库的条款（最终产物）
        self.stack: dict[int, dict] = {}   # level → 累积中的条款

    def build(self, elements: list[dict]) -> list[dict]:
        """建树主入口：逐元素建树 → flush 余栈 → 排序 → 术语章修正。

        功能:
            驱动整条建树流水线，是本类唯一对外的建树方法。
        参数:
            elements (list[dict]): ``read_v1`` 产出的规范化元素列表，每个元素形如
                ``{type, text, page, is_heading, raw, body?, img_path?}``。
        返回:
            list[dict]: 扁平条款列表（已排序、已富化），同 ``self.clauses``。每条结构
                见 ``_make_clause``。
        """
        for elem in elements:
            self._consume(elem)
        # 遍历结束，栈里仍累积着的条款全部落库
        for clause in self.stack.values():
            self._flush(clause)
        self._sort()
        self._fix_terminology_chapter()
        return self.clauses

    # ---- 建树辅助（纯函数，无状态，做成 staticmethod）-----------------------

    @staticmethod
    def _clause_level(num_str: str) -> int:
        """计算条款层级。

        功能:
            层级 = 条号的小数点段数，用于层级栈归位。
        参数:
            num_str (str): 条款号，如 ``5.3.4`` / ``1`` / ``E.1.1``（字母前缀也按段数计）。
        返回:
            int: 层级，如 ``5.3.4`` → 3、``1`` → 1、``E.1.1`` → 3。
        """
        return len(num_str.split("."))

    @staticmethod
    def _clause_match(text: str, is_heading: bool) -> re.Match | None:
        """识别正文条款编号。

        功能:
            匹配行首的正文数字条号，并按是否标题做收紧，避免误识别。规则：
            - ``X.X`` / ``X.X.X`` / ``X.X.X.X``（有小数点）：任何元素都可匹配；
            - 纯数字（如 "1 总则"）：仅当明确是标题（is_heading=True）时匹配，
              避免把年份（2006年）、页码等误识别为章号。
        参数:
            text (str): 待匹配的元素文本。
            is_heading (bool): 该元素是否被标记为标题（MinerU 的 text_level）。
        返回:
            re.Match | None: 命中则返回 match 对象（``group(1)`` 为条号），否则 None。
        """
        m = CLAUSE_NUM_RE.match(text)
        if m is None:
            return None
        num = m.group(1)
        if "." not in num and not is_heading:
            return None
        return m

    def _make_clause(self, path: str, level: int, content: str, page: int,
                     title: str | None = None) -> dict:
        """新建一个空条款节点（正文/附录共用）。

        功能:
            产出统一 schema 的条款 dict；``is_mandatory`` / ``references_to`` 等留待
            ``_flush`` 落库时再填，``standard_id`` 取自实例。
        参数:
            path (str): 条款号 / clause_path，如 ``5.3.4`` / ``附录E``。
            level (int): 条款层级（见 ``_clause_level``）。
            content (str): 条款正文（后续可被 ``_attach`` 追加）。
            page (int): 条款首次出现的页码（从 1 起）。
            title (str | None): 标题；缺省则用 ``path``（附录根传整行标题）。
        返回:
            dict: 条款节点，含 standard_id / clause_path / level / title / content /
                tables / images / page / is_mandatory / references_to / applicable_scope。
        """
        return {
            "standard_id": self.standard_id,
            "clause_path": path,
            "level": level,
            "title": title or path,
            "content": content,
            "tables": [],
            "images": [],
            "page": page,
            "is_mandatory": False,
            "references_to": [],
            "applicable_scope": {},
        }

    # ---- 状态操作 ----------------------------------------------------------

    def _current(self) -> dict | None:
        """取当前条款（栈顶）。

        功能:
            返回层级最深的、正在累积内容的条款，供 ``_attach`` 挂载内容。
        参数:
            无。
        返回:
            dict | None: 栈顶条款；空栈返回 None。
        """
        return self.stack[max(self.stack)] if self.stack else None

    def _flush(self, clause: dict) -> None:
        """条款定稿落库。

        功能:
            为条款补算强制性与交叉引用后追加进 ``self.clauses``；空条款（无正文且
            无表格）直接丢弃。
        参数:
            clause (dict): 待落库的条款节点（``_make_clause`` 产出、可能已被追加内容）。
        返回:
            无（就地 append 到 ``self.clauses``）。
        """
        text = clause.get("content", "").strip()
        if not text and not clause.get("tables"):
            return
        clause["is_mandatory"] = is_mandatory(text)
        clause["references_to"] = extract_references(text)
        self.clauses.append(clause)

    # ---- 单元素分派 --------------------------------------------------------

    def _consume(self, elem: dict) -> None:
        """处理单个规范化元素（建树主循环的一步）。

        功能:
            先拦目录短行，再识别条号——命中正文/附录条号则开新条款，否则把内容挂到
            当前条款。是 build() 里逐元素调用的分派器。
        参数:
            elem (dict): 一个规范化元素 ``{type, text, page, is_heading, ...}``。
        返回:
            无（就地改 ``self.stack`` / ``self.clauses``）。
        """
        text = elem["text"]
        is_heading = elem.get("is_heading", False)

        # 目录条目（短标题 + 尾随页码，如 "9 合同价款期中支付 (35)"）不建条款——
        # 否则与正文真实条款重复污染。覆盖章节级/附录级、中英文 TOC（list 与 text 来源都拦）。
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
        """开一条新条款（正文或附录字母条号）。

        功能:
            遇正文数字条号 "5.3.4" 或附录字母条号 "E.1.1" 时，按层级把同级及更深的旧
            条款落库，再压入新条款；交叉引用片段（如 "8.3节…"）跳过不建。
        参数:
            text (str): 当前元素文本（含条号 + 正文）。
            m (re.Match | None): 正文条号 match（``_clause_match`` 结果），可能为 None。
            appc_m (re.Match | None): 附录字母条号 match，可能为 None；m 与 appc_m 至少一个非空。
            elem (dict): 当前规范化元素（取 page 等）。
        返回:
            无（就地改 ``self.stack`` / ``self.clauses``）。
        """
        # 层级都按小数点段数计（附录根"附录E"为 level 1，E.1→2，E.1.1→3，栈按 level 归位，与正文一致）。
        path = m.group(1) if m else appc_m.group(1)

        # 交叉引用片段，非标题：形如 "8.3节、第8.9节…"（MinerU 把前导"第"切到上一元素）。
        # 判据：条号后**紧跟**节/条/款/项 且无空格分隔；真标题数字后有空格("8.3 暂列金额")。
        if text[len(path):len(path) + 1] in "节条款项":
            return

        lvl = self._clause_level(path)
        body = text[len(path):].strip()  # 条款号后的正文

        # 同级及更深的旧条款先落库，再把新条款压栈
        for k in [k for k in self.stack if k >= lvl]:
            self._flush(self.stack.pop(k))
        self.stack[lvl] = self._make_clause(path, lvl, body, elem["page"])

    def _open_appendix_root(self, app_m: re.Match, text: str, elem: dict) -> None:
        """开一个附录根节点。

        功能:
            遇附录根"附录E"时，清空整栈（让正文段落收尾落库），再建一个 level 1 的附录
            节点，后续 ``E.1`` / ``E.1.1`` 会挂在它之下。
        参数:
            app_m (re.Match): 附录根 match（``group(1)`` 为字母，如 "E"）。
            text (str): 整行标题文本，用作附录节点的 title。
            elem (dict): 当前规范化元素（取 page）。
        返回:
            无（就地改 ``self.stack`` / ``self.clauses``）。
        """
        for k in list(self.stack):
            self._flush(self.stack.pop(k))
        self.stack[1] = self._make_clause(
            f"附录{app_m.group(1)}", 1, "", elem["page"], title=text
        )

    def _attach(self, elem: dict, text: str) -> None:
        """把非条号元素挂到当前条款。

        功能:
            表格 → 当前条款的 ``tables``；图片 → ``images``；其余文字 → 追加进
            ``content``。无当前条款（栈空）则丢弃。
        参数:
            elem (dict): 当前规范化元素（type 决定挂载方式，含 body/img_path/page）。
            text (str): 元素文本（表格/图片时是 caption，文字时是正文）。
        返回:
            无（就地改栈顶条款）。
        """
        cur = self._current()
        if cur is None:
            return

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

    # ---- 收尾 --------------------------------------------------------------

    @staticmethod
    def _sort_key(c: dict) -> tuple:
        """计算条款的排序键。

        功能:
            把 clause_path 映射成可比较的 5 元组，首位分区把正文/附录/异常分层排开。
        参数:
            c (dict): 条款节点（读 ``clause_path``）。
        返回:
            tuple: 5 元组排序键，首位分区 0=正文(数字号) < 1=附录(根 + 字母号 E.1.1) < 2=异常。
        """
        path = c["clause_path"]
        root = re.match(r"^附录\s*([A-Z])$", path)
        if root:
            return (1, ord(root.group(1)), 0, 0, 0)          # 附录根，排在同字母条款之前
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
        """按条款编号重排 ``self.clauses``。

        功能:
            修 MinerU 某些页内顺序颠倒的问题，使条款按 正文 < 附录 < 异常 排列。
        参数:
            无。
        返回:
            无（就地排序 ``self.clauses``）。
        """
        self.clauses.sort(key=self._sort_key)

    def _fix_terminology_chapter(self) -> None:
        """修正术语章（第2章）的强制性误判。

        功能:
            术语章（clause_path 以 "2." 开头）多为定义性文字，含"应"不应算强条；这里
            把它们的 ``is_mandatory`` 收紧为只认硬强条词（必须/严禁/不应/不得/禁止）。
        参数:
            无。
        返回:
            无（就地改 ``self.clauses`` 中相关条款的 is_mandatory）。
        """
        for c in self.clauses:
            if c["clause_path"].startswith("2."):
                c["is_mandatory"] = bool(
                    re.search(r"必须|严禁|不应|不得|禁止", c.get("content", ""))
                )

    # ---- 统计报告 ----------------------------------------------------------

    def print_stats(self) -> None:
        """打印条款树统计报告。

        功能:
            用 rich 表格打印总数、强条数、含表/含引用条款数、各层级分布。纯展示、不改
            数据，是解析后的健全性体检（条款数异常 / 强条为 0 / 层级分布异常都能一眼看出）。
            需在 ``build()`` 之后调用。
        参数:
            无（读 ``self.clauses``）。
        返回:
            无（打印到终端）。
        """
        clauses = self.clauses
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
    help="MinerU 输出的 _content_list.json（v1）路径。",
)
@click.option("--standard-id", default="", help="规范标识；不传则取输入文件名 basename。")
@click.option(
    "--output-dir", "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
)
@click.option("--preview", is_flag=True, help="只打印前 20 条，不写文件。")
def main(input_path: Path, standard_id: str, output_dir: Path, preview: bool) -> None:
    """从 MinerU content_list.json（v1）构建结构化条款库。"""

    # 唯一标识与输出文件名同源：都取输入 basename（去掉 _content_list 后缀）。
    base = input_path.stem.replace("_content_list", "")
    if not standard_id:
        standard_id = base

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]输入：[/bold]{input_path}")

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    elements = read_v1(data)
    console.print(f"共 {len(elements)} 个元素，开始解析…")

    builder = ClauseTreeBuilder(standard_id)
    clauses = builder.build(elements)
    console.print(f"[green]✓ 提取到 {len(clauses)} 条条款[/green]")

    builder.print_stats()

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
    out_path = output_dir / f"{base}_clauses.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clauses, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 已写入 {out_path}[/green]")


if __name__ == "__main__":
    main()
