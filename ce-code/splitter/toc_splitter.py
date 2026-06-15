"""TocSplitter —— 基于 PDF 原生目录(TOC)的多层级切分（核心设计原则 1，当前默认实现）。

吃解析层**格式归一**的 ``Document``（blocks 为纯版面块、**未打标**），本层先**目录打标**
（CatalogLabeler，§0）给每块打目录归属标签 + 解析有序目录条目表，再以目录条目为骨架建保留
parent/child 的语义树。按职责分四段排布：

  §0 目录打标（``CatalogLabeler`` / ``print_catalog_stats``）——格式归一块 → 带目录标签的块 + 条目表。
  §1 引用图分型（``extract_references`` / ``annotate_references``）——散文 → 分型引用边 + 反向边。
  §2 建树（``_BuildNode`` / ``classify_heading`` / ``TreeBuilder``）——目录条目骨架 + 正文挂载 → Chunk 树。
  §3 切分策略（``TocSplitter``）——把 §0→§2 收口成 Splitter 契约。

切分主链（``TocSplitter.split`` → ``TreeBuilder.apply``）：以目录条目为骨架建树——条目物化为骨架
节点（根治父链断裂），正文按条文号号段 / 目录归属挂载，连边后算定固有事实（祖先链 / 引用图分型 +
反向边）；无目录页时退化为复用 MinerU 标题层级 best-effort。``_BuildNode`` 字段名即与 Chunk 对齐，
出口 ``to_chunk()`` 一步成 Chunk，无字段改名缝。

切分深度两把闸（``split`` 只收两个会被读取的参数）：
  · ``max_depth: int | None`` —— 切到第几级**目录**（按 node_path 号段层级，1=章/2=节/3=条…）；
    超过的目录条目/标题不单独建节点，正文并入最近祖先。None=全目录深度。
  · ``subsplit: str`` —— ``"none"``（镜像目录，不细分）/ ``"number"``（在目录骨架之下按编号号段
    再切出更细的编号子节点，节/条/款/项皆可、不专指条）。二者正交，组合即「控制切到哪一层」。

依赖：``ir.chunk`` / ``ir.document``（IR，绝对 import）。仅 stdlib + rich，无外部服务。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import click
from rich.console import Console
from rich.table import Table

from ir.chunk import Chunk, Provenance, Reference  # 出口 IR + 溯源/引用边
from ir.document import Document
from splitter.base import Splitter, SplitResult

console = Console()


def _write_json(data: list, path: Path) -> None:
    """落盘 JSON（UTF-8、缩进、不转义中文），供 ``TocSplitter.run_cli`` 写 chunks/catalog_blocks。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 写入 {path}（{len(data)} 条）[/green]")


# ===========================================================================
# §0 目录打标 —— CatalogLabeler（建树前第一步：给每块打目录归属标签 + 解析条目表）
# ===========================================================================
#
# 格式归一（parser FormatAdapter）之后、建树（§2）之前的一步：给每块打一个可靠的「目录归属」标签，
# 并解析出**有序目录条目表**（骨架真值）供 §2 建树。本步只产目录标签、不建树（条文号 / 树边是 §2
# 的事），对 MinerU 不做减法（目录页只标不删，目录判定采区域判据，不据单行启发式丢正文块）。
#
# 定位（方案 5 混合）：目录页解析成有序条目表作骨架，正文按文档序扫描、归一化匹配条目切换「当前
# 条目」，未命中的块归属最近命中条目（条 x.x.x 归到其节 x.x）；无目录页时退化为以 MinerU 标题块
# 自身标题作边界。
#
# 输入：解析层格式归一产出的统一元素列表（``Document.block_dicts()``）。输出：annotate() 返回每块带
# catalog + catalog_source 的扁平列表（list 块逐条展开、不丢块），喂给 §2 TreeBuilder；``self.entries``
# 存有序条目表（骨架真值）。每块字段：
#     block_idx       int   该块在 MinerU 原始 content_list.json 里的下标（溯源用）。
#     type/text/page  —     原始内容（text 对 list 为空、对 table 为 caption）。
#     text_level      int?  MinerU 标题层级，仅标题块有（本层只读用于匹配）。
#     standard_id     str   规范标识。
#     catalog         值    目录归属："toc" / 条目标题 / None。
#     catalog_source  str   catalog 来源审计，五取值：
#                             toc_page         本块是目录页（区域判据命中）。
#                             toc_match        正文块命中目录条目，确认为标题边界。
#                             inherited        未命中，继承最近命中条目。
#                             heading_fallback 无目录页退化，用 text_level 标题块自身标题作边界。
#                             front_matter     封面/扉页/前言等前置内容（首个条目命中前，catalog=None）。
#
# 目录打标快照 catalog_blocks.json（调试，下游不读）由 ``TocSplitter.split`` 经 ``SplitResult.catalog_blocks``
# 顺带返回，``print_catalog_stats`` 打按来源分解的统计。仅 stdlib + rich，无外部服务。


# 目录页块判定 / 条目页码剥离用：匹配「行尾页码引用」的三种形态。
#   （23）/(23)            括号页码
#   …… 23 / .... 23        点 / 省略号导引 + 页码
#   标题   23              仅尾随空白 + 页码（靠区域判据兜住误伤，见 _mark_toc）
_CATALOG_TAIL_RE = re.compile(
    r"(?:[（(]\s*\d{1,4}\s*[）)]"
    r"|[.．·•…\s]{2,}\d{1,4}"
    r"|\s+\d{1,4})\s*$"
)

# 归一化匹配用：全角数字/点 → 半角，便于正文标题与目录条目对齐。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．", "0123456789.")

# 目录区域：连续 entry_like 文本行需达此长度才判为目录（避开 2~3 行误伤）。
MIN_TOC_RUN = 4
# 目录定位：正文块向后匹配目录条目的前瞻窗口（容忍若干条目在正文无对应块）。
TOC_LOOKAHEAD = 8


def _norm(text: str) -> str:
    """归一化文本用于匹配：全角数字/点转半角、去所有空白（含全角空格）。"""
    return re.sub(r"\s+", "", text.translate(_FULLWIDTH_DIGITS))


def _split_catalog_line(text: str) -> tuple[str, int | None]:
    """目录条目行 → (干净标题, 页码)。剥掉尾部页码引用与导引点。

    如 "5.3 防火分区 …… 23" → ("5.3 防火分区", 23)；无尾页码时页码 None、标题为整行去空白。
    """
    m = _CATALOG_TAIL_RE.search(text)
    page: int | None = None
    if m:
        pm = re.search(r"\d{1,4}", m.group(0))
        page = int(pm.group()) if pm else None
        title = text[: m.start()]
    else:
        title = text
    return title.rstrip(" .．·•…　\t").strip(), page


def _is_catalog_list(items: list[str], thresh: float = 0.5) -> bool:
    """目录页列表判定：过半条目以「页码」结尾则整列为目录页（识别 MinerU 把整个目录抽成单个 list）。"""
    if not items:
        return False
    hits = sum(1 for x in items if _CATALOG_TAIL_RE.search(x.strip()))
    return hits >= max(1, len(items)) * thresh


class CatalogLabeler:
    """目录打标器：给每块打 `catalog`（"toc" / 所属目录条目标题 / None）+ catalog_source 审计。

    流程（方案 5 混合）：① 展平 + 盖章 standard_id（list 条目逐条展开，不丢块）；② 目录页识别
    （区域判据）：整列目录或连续成行 entry_like（run ≥ MIN_TOC_RUN）→ catalog="toc"；③ 解析有序
    目录条目表（骨架真值，存 self.entries）；④ 目录定位：正文按文档序扫描、归一化匹配条目切换
    「当前条目」（无目录页时退化为以 text_level 标题块自身标题作边界）。text_level 由解析层格式归一
    原样透传，本层只读用于匹配、不重打。
    """

    def __init__(self, standard_id: str) -> None:
        self.standard_id = standard_id
        self.entries: list[dict] = []  # annotate() 后填：有序目录条目表（骨架真值，供建树器取）

    def annotate(self, elements: list[dict]) -> list[dict]:
        """展平 → 目录打标 → 目录定位，逐块写 standard_id + catalog + catalog_source。

        副作用：把解析出的有序目录条目表存到 ``self.entries``（``{title, norm, page}``），供
        TreeBuilder 物化骨架（无目录页时为空列表）。
        """
        blocks = self._flatten(elements)
        self._mark_toc(blocks)
        self.entries = self._parse_entries(blocks)
        self._locate(blocks, self.entries)
        return blocks

    def _flatten(self, elements: list[dict]) -> list[dict]:
        """展平为块列表、盖章 standard_id；整列目录的 list 条目直接标 catalog="toc"。

        list 块逐条展开成块（展开后丢弃 list_items，不逐块冗余保留）。
        """
        out: list[dict] = []
        for elem in elements:
            base = {**elem, "standard_id": self.standard_id}
            if elem["type"] == "list":
                items = base.pop("list_items", [])
                toc = _is_catalog_list(items)  # 整列目录页 → 每条盖 catalog="toc"
                for sub in items:
                    b = {**base, "text": sub}
                    if toc:
                        b["catalog"] = "toc"
                    out.append(b)
            else:
                out.append(base)
        return out

    @staticmethod
    def _entry_like(block: dict) -> bool:
        """单块是否「像一条目录行」：text/list 短行且行尾带页码引用（区域判据的逐块信号）。"""
        if block.get("catalog") == "toc":
            return True
        text = block.get("text", "")
        return (
            block.get("type") in ("text", "list")
            and len(text) < 80
            and bool(_CATALOG_TAIL_RE.search(text))
        )

    def _mark_toc(self, blocks: list[dict]) -> None:
        """区域判据标目录页：连续 entry_like 成行（run ≥ MIN_TOC_RUN）整段标 catalog="toc"。

        只把**成片**的目录行判为目录，孤立的「行尾带数字」正文短行不误判、不丢弃（守「不做减法」）。
        整列目录已在 _flatten 直接盖章，本步补「逐行排版的目录」。
        """
        n = len(blocks)
        i = 0
        while i < n:
            if not self._entry_like(blocks[i]):
                i += 1
                continue
            j = i
            while j < n and self._entry_like(blocks[j]):
                j += 1
            if j - i >= MIN_TOC_RUN:
                for k in range(i, j):
                    blocks[k]["catalog"] = "toc"
            i = j

    @staticmethod
    def _parse_entries(blocks: list[dict]) -> list[dict]:
        """从 catalog="toc" 的块解析有序目录条目表，每条 {title, norm, page}（norm 供定位匹配）。"""
        entries: list[dict] = []
        for b in blocks:
            if b.get("catalog") != "toc":
                continue
            title, page = _split_catalog_line(b.get("text", ""))
            if title:
                entries.append({"title": title, "norm": _norm(title), "page": page})
        return entries

    @staticmethod
    def _locate(blocks: list[dict], entries: list[dict]) -> None:
        """目录定位：给每块写 catalog（所属条目标题）+ catalog_source（来源审计）。

        目录页块 source=toc_page。有目录条目时维护单调前瞻指针，正文块归一化文本命中
        entries[ptr:ptr+W] 中某条（精确相等，或 text_level 标题块前缀相等）即切换「当前条目」、
        指针前移（source=toc_match）；未命中则继承当前条目（source=inherited，首个命中前
        catalog=None、source=front_matter）。无目录条目时退化：text_level 标题块自身标题作边界
        （source=heading_fallback），其余继承。
        """
        cur: str | None = None
        ptr = 0
        for b in blocks:
            if b.get("catalog") == "toc":
                b["catalog_source"] = "toc_page"
                continue
            if entries:
                nb = _norm(b.get("text", ""))
                hit = None
                for j in range(ptr, min(ptr + TOC_LOOKAHEAD, len(entries))):
                    en = entries[j]["norm"]
                    if len(en) < 2:
                        continue
                    if nb == en or (b.get("text_level") is not None and nb.startswith(en)):
                        hit = j
                        break
                if hit is not None:
                    ptr = hit + 1
                    cur = entries[hit]["title"]
                    b["catalog"], b["catalog_source"] = cur, "toc_match"
                else:
                    b["catalog"] = cur
                    b["catalog_source"] = "inherited" if cur is not None else "front_matter"
            elif b.get("text_level") is not None and b.get("text"):  # 退化：标题块自身作边界
                cur = b["text"]
                b["catalog"], b["catalog_source"] = cur, "heading_fallback"
            else:
                b["catalog"] = cur
                b["catalog_source"] = "inherited" if cur is not None else "front_matter"


def print_catalog_stats(rows: list[dict]) -> None:
    """打印目录打标统计：总块/标题数 + 按 catalog_source 分解各来源占比。"""
    total = len(rows)
    headings = sum(1 for e in rows if e.get("text_level") is not None)
    src = Counter(e.get("catalog_source") for e in rows)
    located = src["toc_match"] + src["inherited"] + src["heading_fallback"]

    t = Table(title="目录打标统计（目录打标 + 定位）")
    t.add_column("指标")
    t.add_column("数量", justify="right")
    t.add_row("总块数", str(total))
    t.add_row("  标题块(text_level)", str(headings))
    t.add_row("  目录页块(toc_page)", str(src["toc_page"]))
    t.add_row("  已定位块(归属某条目)", str(located))
    t.add_row("    命中目录条目(toc_match)", str(src["toc_match"]))
    t.add_row("    继承(inherited)", str(src["inherited"]))
    t.add_row("    无目录退化(heading_fallback)", str(src["heading_fallback"]))
    t.add_row("  前置内容块(front_matter：封面/扉页/前言，首个目录条目命中前)", str(src["front_matter"]))
    console.print(t)


# ===========================================================================
# §1 引用图分型 —— 散文 → 分型引用边 + 双向（PRD §1/§4 的【P0】引用边）
# ===========================================================================
#
# 替代 02 旧的扁平 ``references_to: list[str]``。引用图是规范的知识图谱、GraphRAG
# 底座,所以分型 + 反向边在构建层一次建好,检索层只按 type 决定是否扩展。
#
#   strong         应符合 / 符合…的规定 / 应按…执行  → 必拉、继承强制性
#   weak           参见 / 见 / 参照 / 宜按            → 可选拉取
#   exclude        不适用于 / 本条不含 / 除…外        → 禁止正向扩展(否则反引)
#   cross_standard 《…》GB/JGJ/CJJ xxxxx              → 触发多规范召回
#
# 裸引用(``第5.2.1条`` 无修饰语)默认 **strong**:规范里裸引用通常是规范性引用,
# 且领域偏好"宁可多召不可漏"。误标的代价(多拉一条)远小于漏强条。

# 与 schema.RefType 保持一致;extract 包对 plain dict 操作,不强依赖 schema 导入路径
RefType = Literal["strong", "weak", "exclude", "cross_standard"]

# 本规范内条款号:可选「第」前缀(g1) + 2~4 段编号(g2) + 可选「条/款/项/节」量词后缀(g3)。
# 至少两段以排除年份/页码/章号。两段 d.d 与正文量值高度同形(6.0m/1.5h/坡度5.3%),故
# extract_references 对**无锚点**(无 第/条款项节)的号再加两道召回安全的精度闸,见该函数。
_NUM_RE = re.compile(r"(第)?\s*(\d+\.\d+(?:\.\d+){0,2})\s*(条|款|项|节)?")

# 号后紧跟量纲单位 → 这是量值不是条款引用(6.0m/1.5h/0.5MPa/5.3%/3.6kN)。召回安全:
# 真实条款引用号后从不接单位。
_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:mm|cm|km|m²|m³|m2|m3|m/s|kN|kPa|MPa|Pa|kg|m|h|s|t|N|%|‰|℃|°"
    r"|毫米|厘米|千米|米|秒|小时|倍)"
)
# 号前紧邻 表/图/式 → 这是表/图/公式引用不是条款引用(表5.3/图5.3/式5.3)。召回安全:
# 真实条款引用号前不会是这三个字。
_FIGURE_LEAD = ("表", "图", "式")

# 跨规范:《建筑设计防火规范》GB 50016-2014 / 现行国家标准…GB 50116
_CROSS_RE = re.compile(
    r"(?:《[^》]*》\s*)?((?:GB|GBJ|JGJ|CJJ|TB|DG|DGJ)\s*/?\s*[T]?\s*\d{4,5}(?:-\d+)?)"
)

# 分型关键词(在引用号前的小窗口里判断)
_EXCLUDE_KW = ("不适用", "本条不含", "本条不适用", "除外")
_WEAK_KW = ("参见", "参照", "宜按", "宜符合", "可按", "可参照", "详见")
_STRONG_KW = ("应符合", "尚应符合", "应按", "应执行", "符合本", "的规定", "应满足")


def _classify(prefix: str) -> RefType:
    """据引用号前文窗口判断引用类型;无修饰语 → 默认 strong(保守召回)。"""
    if any(k in prefix for k in _EXCLUDE_KW):
        return "exclude"
    if any(k in prefix for k in _STRONG_KW):
        return "strong"
    if any(k in prefix for k in _WEAK_KW):
        return "weak"
    return "strong"


def extract_references(text: str, self_path: str = "") -> list[dict]:
    """散文 → 分型引用边列表 ``[{to, type}]``,按 (to,type) 去重、剔除自引。"""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # 排除句首已被《》圈起的跨规范号区间,避免本规范号正则误抓跨规范里的数字
    cross_spans: list[tuple[int, int]] = []
    for m in _CROSS_RE.finditer(text):
        to = re.sub(r"\s+", " ", m.group(1)).strip()
        cross_spans.append(m.span())
        key = (to, "cross_standard")
        if key not in seen:
            seen.add(key)
            found.append({"to": to, "type": "cross_standard"})

    for m in _NUM_RE.finditer(text):
        if any(s <= m.start() < e for s, e in cross_spans):
            continue  # 跨规范号里的内部条号,已由 cross 边覆盖
        to = m.group(2)
        if to == self_path:
            continue
        prefix = text[max(0, m.start() - 10): m.start()]
        anchored = bool(m.group(1) or m.group(3))  # 第… / …条款项节:确是条款引用
        if not anchored:
            # 无锚点号:两道召回安全的精度闸,滤掉与条款号同形的量值/表图引用。
            lead = prefix.rstrip()
            if lead and lead[-1] in _FIGURE_LEAD:
                continue  # 表5.3 / 图5.3 / 式5.3
            if _UNIT_AFTER_RE.match(text[m.end():]):
                continue  # 6.0m / 1.5h / 5.3%
        rtype = _classify(prefix)
        key = (to, rtype)
        if key not in seen:
            seen.add(key)
            found.append({"to": to, "type": rtype})
    return found


def build_referenced_by(nodes: list) -> None:
    """全量扫描 references,原地回填每个节点 ``referenced_by``(仅本规范内边)。

    入参为带 ``node_path`` / ``references`` / ``referenced_by`` 属性的节点对象
    (建树期 ``_BuildNode``;鸭子类型,不强依赖具体类)。``references`` 元素仍是
    ``{to,type}`` dict。
    """
    paths = {c.node_path for c in nodes}
    reverse: dict[str, list[str]] = {}
    for c in nodes:
        src = c.node_path
        for ref in c.references:
            tgt = ref["to"]
            if ref["type"] == "cross_standard" or tgt not in paths:
                continue
            reverse.setdefault(tgt, [])
            if src not in reverse[tgt]:
                reverse[tgt].append(src)
    for c in nodes:
        c.referenced_by = reverse.get(c.node_path, [])


def annotate_references(nodes: list) -> None:
    """原地富化:为每个节点(带 ``content`` / ``node_path`` / ``references`` / ``referenced_by``
    属性,见 ``_BuildNode``)写 ``references``(分型)+ ``referenced_by``(反向)。

    只产 typed ``references``/``referenced_by``;检索期扩展用的扁平 ``references_to``
    由 ``retrieval/indexer._expandable_refs`` 在建索引时从 typed 边派生(strong/cross_standard
    才入),不在本层落字段。
    """
    for c in nodes:
        c.references = extract_references(c.content, c.node_path)
    build_referenced_by(nodes)


# ===========================================================================
# §2 建树 —— TreeBuilder（目录条目骨架 + 正文挂载 → Chunk 树）
# ===========================================================================
#
# 读 §0 目录打标产出的块（带 catalog/catalog_source）+ 有序目录条目表（entries），以**目录条目
# 为骨架**还原成保留 parent/child 的语义树（``chunks.json``，单一真值）。建树策略：
#
#   ① **目录条目物化骨架**：每个 TOC 条目 → 一个骨架节点（**恒存在**，即使正文里没有对应正文块），
#      条目标题跑 ``classify_heading`` 取 node_path。这是「父链断裂」的根治——上层章/节不再因
#      「只有标题没正文」被丢。
#   ② **条目按号段嵌套**：「5.3 属于 5」靠 node_path 号段（``_resolve_parent``）；号段失效
#      （无编号散文）则回退到 catalog 归属。
#   ③ **正文挂载 + 两把切分深度闸**（``max_depth`` / ``subsplit``）：正文标题块若 node_path 命中
#      骨架节点 → **接地**（补 provenance / 正文 / 表格）。不命中时：
#        · ``subsplit=="none"``（默认，树严格镜像目录）→ 目录没列的更细标题不建节点，归入其所属
#          骨架节点（号段最近祖先）的正文，block_idx 完整留在该节点 provenance 下。
#        · ``subsplit=="number"`` → 把「以编号号段开头」的标题/正文块（号段含小数点、排除交叉引用）
#          另起编号子节点，号段自动挂到最近祖先骨架，block_idx 随块带入。
#      ``max_depth`` 另限**目录骨架**的号段层级：超过者不建骨架、正文并入最近祖先。两闸正交：
#      max_depth 限目录树深度，number 在其下补更细单元（编号子节点不受 max_depth 限）。
#   ④ **固有事实一次算定**：祖先链（``_attach_ancestors``）、引用图分型 + 反向边（§1）。
#
#   无目录页时（entries 为空）退化为「号段建树」best-effort：正文标题块按号段连边、空骨架不丢。
#   **限制**：``subsplit=="none"`` 时目录没列的更细单元（如 5.3.4）不是独立节点 → 对**该层**目标的
#   ``referenced_by`` / 引用扩展会落空（节级正常）；置 ``subsplit=="number"`` 即建子节点恢复。
#
# 内部 IR：建树期节点用 ``_BuildNode`` dataclass（字段名即与 ``ir.chunk.Chunk`` 对齐，消灭出口
# 改名缝），另带两个建树期临时字段 ``catalog`` / ``skeleton``（``to_chunk()`` 时丢弃）。
# 输入：① 目录打标块列表（带 text_level / catalog / catalog_source / block_idx 溯源）；② entries
# （有序目录条目表，骨架真值）。输出：Chunk 树（含 parent_id/children_ids + 祖先链 + 引用图）。


@dataclass
class _BuildNode:
    """建树期节点（结构层内部 IR）——字段名即与 ``ir.chunk.Chunk`` 对齐，消灭出口改名缝。

    与 Chunk 同名同义的结构字段（parent_id / ancestor_paths / provenance:Provenance …）建树末算定；
    另带两个**建树期临时字段** ``catalog`` / ``skeleton``，仅供连边/剪枝用，``to_chunk()`` 时
    丢弃、不进 chunks.json。``references`` 建树期内持 ``{to,type}`` dict（``extract_references``
    产物），``to_chunk()`` 时转 ``Reference``。
    """

    # 字段顺序镜像 ir.chunk.Chunk（标识→载荷→结构→引用图→溯源→表格图示→审计）；chunk_id 不落
    # 建树期，由 Chunk.__post_init__ 派生（standard_id#node_path）。
    node_path: str
    standard_id: str = ""
    title: str = ""
    content: str = ""
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    ancestor_titles: list[str] = field(default_factory=list)
    ancestor_paths: list[str] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)   # {to,type}；to_chunk() 转 Reference
    referenced_by: list[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    tables: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    node_path_source: str = ""        # number / text_level（来自 classify_heading）
    node_path_confidence: float = 1.0
    # ── 建树期临时（不进 Chunk）──
    catalog: str | None = None        # 本节点对应/所属目录条目标题（catalog 兜底连边用）
    skeleton: bool = False            # 目录条目物化的骨架节点，恒保留（压过空正文剪枝）

    def to_chunk(self) -> Chunk:
        """建树期节点 → Chunk IR（丢弃 catalog/skeleton；references dict → Reference）。

        参数：
            无。
        返回：
            Chunk: 出口 IR（features 留空，由表征层 enrich 挂）。
        """
        return Chunk(
            node_path=self.node_path,
            standard_id=self.standard_id,
            title=self.title,
            content=self.content,
            parent_id=self.parent_id,
            children_ids=self.children_ids,
            ancestor_titles=self.ancestor_titles,
            ancestor_paths=self.ancestor_paths,
            references=[Reference.from_dict(r) for r in self.references],
            referenced_by=self.referenced_by,
            provenance=self.provenance,
            tables=self.tables,
            images=self.images,
            node_path_source=self.node_path_source,
            node_path_confidence=self.node_path_confidence,
        )


# ---------------------------------------------------------------------------
# 条文号正则（从标题文字识别条款号）
# ---------------------------------------------------------------------------

CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[　 一-鿿]")
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")
APPENDIX_CLAUSE_RE = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,2})\s*[一-鿿]")

# ---------------------------------------------------------------------------
# 附录根识别（父路径推断用：附录E → 顶层、E.1 → 附录E）
# ---------------------------------------------------------------------------

_APPENDIX_ROOT_RE = re.compile(r"^附录\s*[A-Z]$")


# ---------------------------------------------------------------------------
# 条文号 → 路径/置信度（建树器调用）
# ---------------------------------------------------------------------------


def classify_heading(text: str) -> dict | None:
    """从一行标题文字识别条款号 / 路径来源 / 置信度 —— **无状态纯函数**（建树器调用）。

    功能：把「这行标题对应哪个条款号、路径来源置信几何」这件纯文本判定独立出来，便于
        单测与复用。**只定 node_path**——节点种类（容器/叶）与深度均为纯派生事实
        （消费方按 children_ids / ancestor_paths 推），不落字段、更不在此推。

        判定顺序（**四级短路**，命中即返回；只看本行文字，不依赖上下文/父节点/页码）：
          ① 「附录X」开头（附录E 钢筋计算）         → node_path="附录"+字母（附录E），number/1.0
          ② 「字母.数字…」开头（E.1.1 ……）         → node_path=该号（E.1.1），number/1.0
          ③ 「数字(可带小数点)+紧跟汉字/空格」开头   → node_path=开头数字号（1 / 5.3 / 5.3.4），
               number/1.0；**但**数字后紧跟「节/条/款/项」者 = 交叉引用片段（"5.3节…"）
               → 返回 None，调用方按内容块处理、不建节点。
          ④ 三者都不中（无编号标题，如 前言 / 术语和定义）→ node_path=标题文字前 30 字，
               text_level/0.6（低置信，进 03 抽查）。
        号 ③ 的"紧跟汉字/空格"约束让 "1总则" 命中而纯页码 "9"/年份 "2024" 不误判为条号
        （但注意：排除引用只看号后**紧邻**字符，"5.3 节"带空格会绕过排除、误当标题——已知边界）。

    参数：
        text (str): 标题块文字（调用方已判定 text_level 存在，即 MinerU 标题块；
            建骨架时亦对目录条目标题调用）。
    返回：
        dict | None: ``{node_path, node_path_source, node_path_confidence}``。
            返回 None 表示该行实为交叉引用片段（如「5.3节…」），应按内容块处理。
            ``node_path_source``：number（命中编号正则，置信 1.0）/ text_level（无编号、
            靠 MinerU 标题标记 + 标题文字兜底作路径，置信 0.6）。
            ⚠ 无编号标题取 ``text[:30]`` 作 node_path，前 30 字相同的两个散文标题会撞 path：
            骨架阶段去重丢后者（``_build_skeleton`` 的 ``path in by_path`` 跳过），无目录页
            （build_missing）时后者并入前者节点（``by_path.get(path)`` 命中即接地）——均非
            数据丢失但是非预期合并，故 text_level 路径置信仅 0.6、进 03 抽查。
    """
    # 附录根（附录A / 附录B）
    app_m = APPENDIX_RE.match(text)
    if app_m:
        return {"node_path": f"附录{app_m.group(1)}",
                "node_path_source": "number", "node_path_confidence": 1.0}

    # 附录字母条号（E.1 / E.2.2）
    appc_m = APPENDIX_CLAUSE_RE.match(text)
    if appc_m:
        return {"node_path": appc_m.group(1),
                "node_path_source": "number", "node_path_confidence": 1.0}

    # 本规范条号（5 / 5.3 / 5.3.4）
    m = CLAUSE_NUM_RE.match(text)
    if m:
        num = m.group(1)
        # "节条款项"后缀说明这是交叉引用片段，非真实条号
        if text[len(num):len(num) + 1] in "节条款项":
            return None
        return {"node_path": num,
                "node_path_source": "number", "node_path_confidence": 1.0}

    # 无编号标题（"前言"、"术语和定义" 等）：用标题文字作路径
    return {"node_path": text[:30].strip(),
            "node_path_source": "text_level", "node_path_confidence": 0.6}


# ---------------------------------------------------------------------------
# 父路径推断（由 node_path 反推父节点路径）
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


def _resolve_parent(path: str, by_path: dict) -> str | None:
    """解析最近的**已存在**祖先路径（中间层级缺节点时继续上探）。

    参数：
        path (str): 当前节点条款路径。
        by_path (dict): node_path → 节点 的映射（仅用 key 判存在，不读值）。
    返回：
        str | None: 最近存在的祖先条款路径；无则 None（顶层）。
    """
    p = _parent_path(path)
    while p is not None and p not in by_path:
        p = _parent_path(p)
    return p


def _path_depth(path: str) -> int:
    """node_path 号段深度（按编号推断，1-base，**不查节点是否存在**）。

    供 ``max_depth`` 切分深度闸用：沿 ``_parent_path`` 链上溯计数。
    '5'→1 / '5.3'→2 / '5.3.4'→3；'附录E'→1 / 'E.1'→2 / 'E.1.1'→3；无编号标题（前言）→1。

    参数：
        path (str): node_path。
    返回：
        int: 号段层级（≥1）。
    """
    depth = 1
    p = _parent_path(path)
    while p is not None:
        depth += 1
        p = _parent_path(p)
    return depth


# ---------------------------------------------------------------------------
# 建树 — TreeBuilder（结构层：目录条目骨架 + 正文挂载 → 保留 parent/child 的 Chunk 树）
# ---------------------------------------------------------------------------


class TreeBuilder:
    """结构层建树：以**目录条目为骨架**建保留 parent/child 的 Chunk 树（单一真值）。

    功能（2026-06-13 改用 catalog 建树）：
        ① 目录条目物化为骨架节点（恒存在，根治「空骨架被丢→父链断裂」）；
        ② 正文标题块并入同号骨架（接地）或建为新条/款节点；
        ③ 连边：号段反推为主、catalog 归属为兜底；
        ④ 算定祖先链 + 引用图分型 + 反向边（PRD §3.1「固有事实」）。
        空骨架节点恒保留，空且无子的正文节点剪除。表格 / 图示暂留节点 tables / images
        字段（过渡），T8 转表征层子节点 + table_struct 表征。

    切分深度两把闸（2026-06-15 引入，控制「切到哪一层」）：
        max_depth (int | None)：目录骨架的**号段层级**上限（1=章/2=节/3=条…）；超过该层级
            的目录条目/标题不单独建节点，正文并入最近祖先（号段最近祖先 _resolve_parent）。
            None=不限（全目录深度）。
        subsplit (str)：目录层**之下**的**按编号细分**——"none"（不细分，树严格镜像目录）/
            "number"（正文中以编号号段开头的块另起编号子节点，号段自动挂到最近祖先；与 max_depth
            正交，编号子节点不受 max_depth 限——max_depth 限目录骨架、number 在其下补更细单元）。
            细分出的单元可能是 节/条/款/项 中任意层（取决于编号深度），统称「编号子节点」，不专指条。

    参数：
        max_depth (int | None): 切分深度上限（号段层级）。None=全深度。
        subsplit (str): "none" / "number"。
    返回：
        调用 apply(annotated, entries=..., ...) 返回 Chunk 树 list[Chunk]。
    """

    def __init__(self, max_depth: int | None = None, subsplit: str = "none") -> None:
        """初始化建树器（校验切分深度两把闸）。

        参数：
            max_depth (int | None): 目录骨架号段层级上限（≥1）；None=全深度。
            subsplit (str): 目录层下按编号细分策略，"none"（不细分）/ "number"（按编号号段再切）。
        返回：
            无。
        异常：
            ValueError: subsplit 非 none/number，或 max_depth 为 <1 的整数。
        """
        if subsplit not in ("none", "number"):
            raise ValueError(f"未知 subsplit={subsplit!r}（可选 none / number）")
        if max_depth is not None and max_depth < 1:
            raise ValueError(f"max_depth 须为 ≥1 的整数或 None，得到 {max_depth!r}")
        self.max_depth = max_depth
        self.subsplit = subsplit

    def _within_depth(self, path: str) -> bool:
        """该 node_path 是否在 max_depth 切分深度内（None=不限，恒 True）。"""
        return self.max_depth is None or _path_depth(path) <= self.max_depth

    def apply(
        self,
        annotated: list[dict],
        *,
        entries: list[dict] | None = None,
        std: str = "",
        source_file: str = "",
    ) -> list[Chunk]:
        """目录条目骨架 + 正文挂载 → Chunk 树（含 parent/child + 引用图 + 祖先链）。

        参数：
            annotated (list[dict]): §0 目录打标产出的块列表（带 catalog/catalog_source/block_idx）。
            entries (list[dict] | None): 有序目录条目表（骨架真值）；None / 空 → 退化为
                「保留骨架 + 号段建树」（无目录页 best-effort）。
            std (str): 规范标识（写入每个节点）。
            source_file (str): 原始 content_list.json 路径（写入 provenance 溯源）。
        返回：
            list[Chunk]: Chunk 树（ir.chunk.Chunk 形态，features 待表征层挂）。
        """
        meta = {"source_file": source_file}

        by_path: dict[str, _BuildNode] = {}  # node_path → 节点（骨架 + 正文，先现先占）
        order: list[_BuildNode] = []         # 节点创建序（≈文档序），供剪枝/连边遍历

        # ① 目录条目 → 骨架节点（恒存在）
        self._build_skeleton(entries or [], std, meta, by_path, order)
        # ② 正文标题块：命中骨架则接地，更细标题（目录没列的条/款）并入所属骨架节点正文
        #    （树严格镜像目录、不建更细节点；建条下沉 Stage 2 ClauseSplitter）；内容块累积进当前节点。
        #    无目录页（entries 空）退化：标题即结构，build_missing 直接建骨架节点 best-effort。
        self._absorb_body(annotated, by_path, std=std, meta=meta, order=order,
                          build_missing=not entries)
        # ③ 连边：号段为主、catalog 兜底
        self._wire_tree(order, by_path)
        # ④ 剪空正文节点（骨架恒留），算祖先链 + 深度 + 种类 + 引用图
        nodes = self._prune(order)
        self._attach_ancestors(nodes)      # ancestor_titles / ancestor_paths（树深度/种类由消费方派生）
        annotate_references(nodes)         # 固有事实：引用图分型 + referenced_by 反向边（§1）

        # page 累积时按块追加,同页多块会重复;去重升序后 page[0]=首页(展示页)。未接地空骨架不额外
        # 打标——它即 ``provenance.block_idx == []``（无原始块 = 目录列了但正文没抽到，
        # Chunk.is_grounded 已声明此不变式）；catalog/skeleton 建树期临时字段经 to_chunk 自然丢弃。
        for n in nodes:
            if n.provenance.page:
                n.provenance.page = sorted(set(n.provenance.page))
        return [n.to_chunk() for n in nodes]

    # -- ① 骨架 ----------------------------------------------------------------

    def _build_skeleton(
        self, entries: list[dict], std: str, meta: dict,
        by_path: dict[str, _BuildNode], order: list[_BuildNode],
    ) -> None:
        """目录条目 → 骨架节点（恒存在，初始 provenance 空 = 未接地；同号条目去重取首现）。

        ``max_depth`` 闸：超过号段层级上限的目录条目不建骨架节点（其正文将在 _absorb_body
        并入最近祖先），从而把目录树「切到第 N 级」。

        参数：
            entries (list[dict]): 有序目录条目表 {title, norm, page}。
            std (str): 规范标识。
            meta (dict): source_file（provenance 溯源用）。
            by_path (dict): node_path → 节点（原地填）。
            order (list): 节点创建序（原地追加）。
        返回：
            无。
        """
        for ent in entries:
            title = ent.get("title", "").strip()
            info = classify_heading(title) if title else None
            if info is None:
                continue  # "toc"标题行 / 交叉引用片段等：不开骨架节点
            path = info["node_path"]
            if not self._within_depth(path):
                continue  # 超过 max_depth：不建骨架，正文并入最近祖先（_absorb_body 处理）
            if path in by_path:
                continue  # 同号条目去重（目录重复列、跨页续行等）
            node = _BuildNode(
                node_path=path, standard_id=std, title=title,
                node_path_source=info["node_path_source"],
                node_path_confidence=info["node_path_confidence"],
                provenance=Provenance(source_file=meta["source_file"]),
                catalog=title,       # 骨架自身即该目录条目
                skeleton=True,       # 恒保留标记
            )
            by_path[path] = node
            order.append(node)

    # -- ② 正文挂载 ------------------------------------------------------------

    def _absorb_body(
        self, annotated: list[dict], by_path: dict[str, _BuildNode],
        *, std: str = "", meta: dict | None = None,
        order: list[_BuildNode] | None = None, build_missing: bool = False,
    ) -> None:
        """正文块按目录骨架归并；两把切分深度闸（max_depth / subsplit）在此生效。

        每个非目录页块跑 classify_heading 取一个候选 node_path ``ci``：
          · 标题块（有 text_level）始终分类；
          · 普通正文块——仅当 ``subsplit=="number"`` 且文本以**编号**开头（number 源、含小数点
            的多段号，排除交叉引用片段）时才分类（按编号细分的取点；编号由条文号正则识别）。
        据 ``ci`` 决策：
          · **命中既有节点**（骨架/已建编号子节点）→ **接地**（补 provenance/page）；标题块文字
            不入 content（它即 title），带编号正文块文字入 content。
          · **不命中**，按以下顺序决定建节点 / 并入正文：
              - 无目录页（``build_missing``）+ 标题 + 在 max_depth 内 → 建骨架节点（标题即结构）。
              - ``subsplit=="number"`` 且为编号号段 → **建编号子节点**（号段自动挂最近祖先，
                **不受 max_depth 限**：max_depth 限目录骨架、number 在其下补更细单元）；带编号正文块整段入 content。
              - 否则（有目录的更细标题 / 超 max_depth 的标题）→ **不建节点**，文字并入所属骨架
                节点（号段最近祖先 _resolve_parent，失败沿用 cur）的 content + 溯源（树镜像目录）。
        非标题/非编号普通块（含表格/图示）累积进当前节点 content / tables / images + provenance；
        catalog=="toc" 的目录页块跳过；首个节点前的游离块丢弃。

        参数：
            annotated (list[dict]): 标注块列表（文档序）。
            by_path (dict): node_path → 节点（原地补 provenance/content；新建时追加节点）。
            std (str) / meta (dict): 建节点用（规范标识 / source_file 溯源）。
            order (list): 节点创建序（新建时原地追加）。
            build_missing (bool): 无目录页退化——标题块直接建骨架节点。
        返回：
            无。
        """
        meta = meta or {}
        order = order if order is not None else []
        by_number = self.subsplit == "number"
        cur: _BuildNode | None = None
        for elem in annotated:
            if elem.get("catalog") == "toc":
                continue  # 目录页块不开节点、不并入正文（catalog_blocks.json 已全量保留 + 溯源）

            text = elem.get("text", "")
            is_heading = elem.get("text_level") is not None
            ci = classify_heading(text) if (is_heading or (by_number and text)) else None
            # 普通块（非标题）只在「编号 + 多段号」时才作细分切点，排除单段号/无编号噪声
            if ci is not None and not is_heading and not (
                ci["node_path_source"] == "number" and "." in ci["node_path"]
            ):
                ci = None

            if ci is not None:
                path = ci["node_path"]
                node = by_path.get(path)        # 命中既有节点（骨架 / 已建编号子节点）？
                is_number_unit = ci["node_path_source"] == "number" and "." in path
                plain_number = not is_heading    # 带编号正文块：text 即该单元正文，title 取号
                add_to_content = plain_number     # 命中既有节点时带编号正文块的文字需入 content
                if node is None:
                    make = (is_heading and build_missing and self._within_depth(path)) \
                        or (by_number and is_number_unit)  # 按编号细分建子节点（不受 max_depth 限）
                    if make:
                        node = _BuildNode(
                            node_path=path, standard_id=std,
                            title=(path if plain_number else text),
                            node_path_source=ci["node_path_source"],
                            node_path_confidence=ci["node_path_confidence"],
                            provenance=Provenance(
                                source_file=meta.get("source_file", ""),
                                block_idx=[elem["block_idx"]] if "block_idx" in elem else [],
                                page=[elem["page"]],
                            ),
                            catalog=elem.get("catalog"),
                        )
                        if plain_number and text:
                            node.content = text  # 带编号正文块整段即该单元正文（含号前缀）
                        by_path[path] = node
                        order.append(node)
                        cur = node
                        continue
                    # 不建节点：文字并入所属骨架节点（号段最近祖先，失败沿用 cur）
                    owner_path = _resolve_parent(path, by_path)
                    node = by_path.get(owner_path) if owner_path else cur
                    if node is None:
                        continue  # 无所属骨架（首个章节前的游离细标题）→ 丢弃
                    add_to_content = True  # 此处文字（细标题 / 带编号正文）是所属节内容
                if "block_idx" in elem:
                    node.provenance.block_idx.append(elem["block_idx"])
                node.provenance.page.append(elem["page"])
                if node.catalog is None:
                    node.catalog = elem.get("catalog")
                if add_to_content and text:
                    node.content = (node.content + "\n" + text).lstrip("\n")
                cur = node
                continue

            if cur is None:
                continue  # 首个标题之前的内容块，无归属节点 → 丢弃

            t = elem.get("type")
            if "block_idx" in elem:
                cur.provenance.block_idx.append(elem["block_idx"])
            cur.provenance.page.append(elem["page"])
            if t == "table":
                cur.tables.append({
                    "caption": text,
                    "body": elem.get("body", []),
                    "page": elem["page"],
                })
            elif t == "image":
                cur.images.append({
                    "path": elem.get("img_path", ""),
                    "caption": text,
                    "page": elem["page"],
                })
            else:
                if text:
                    cur.content = (cur.content + "\n" + text).lstrip("\n")

    # -- ③ 连边 ----------------------------------------------------------------

    @staticmethod
    def _wire_tree(order: list[_BuildNode], by_path: dict[str, _BuildNode]) -> None:
        """原地连边：号段反推为主、catalog 归属为兜底，回填 children_ids。

        功能：
            每个节点先按 node_path 号段反推最近**已存在**祖先（_resolve_parent）；
            号段反推不到（无编号散文 / 目录与编号不一致）则回退到「它 catalog 所属
            目录条目」对应的骨架节点（按条目标题匹配，且不自指）。条款内层级（5.3.4.1
            归 5.3.4）由号段决定——catalog 只定位到节深，故不参与条内嵌套。

            ⚠ **catalog 兜底假定标题在全规范唯一**：``by_title`` 取首现（setdefault），故若
            「一般规定 / 术语」等重复节标题走到兜底（仅号段失效时触发，带编号节通常不会），
            会一律挂到首个同名节点、错挂父。带编号节点几乎都靠号段解析，触发率低；无编号散文
            才有此风险，待 Stage 2 按位置消歧。

        参数：
            order (list[_BuildNode]): 全部节点（骨架 + 正文，创建序）。
            by_path (dict): node_path → 节点。
        返回：
            无（原地写 parent_id / children_ids）。
        """
        by_title: dict[str, _BuildNode] = {}
        for n in order:
            by_title.setdefault(n.title, n)  # catalog 兜底用：目录条目标题 → 骨架节点
            n.children_ids = []
        for n in order:
            parent: _BuildNode | None = None
            pp = _resolve_parent(n.node_path, by_path)
            if pp is not None:
                parent = by_path.get(pp)
            if parent is None and n.catalog:  # 号段失效 → catalog 归属兜底
                cand = by_title.get(n.catalog)
                if cand is not None and cand.node_path != n.node_path:
                    parent = cand
            n.parent_id = parent.node_path if parent is not None else None
            if parent is not None:
                parent.children_ids.append(n.node_path)

    # -- ④ 剪枝 + 祖先链 -------------------------------------------------------

    @staticmethod
    def _prune(order: list[_BuildNode]) -> list[_BuildNode]:
        """剪除空正文节点（骨架恒留），级联剪到稳定；原地修父节点 children_ids。

        功能：节点保留条件 = 骨架 / 有正文 / 有表格 / 有存活子节点。剪一个空叶可能令其
            父变空叶，故循环到稳定。删除时从父 children_ids 摘除，保证树一致。

            ⚠ **未接地空骨架的叶子会留下**（``skeleton`` 恒留压过空正文）：容器骨架留作父链
            是对的，但 ``provenance.block_idx==[]`` 且无子的骨架（目录列了、正文从未
            抽到块）``content`` 为空，对检索是死单元。**契约**：下游 view / index 须跳过
            「``block_idx==[]`` 且无子节点（叶）」的空骨架，勿 emit 成空检索单元
            （见 Chunk.is_grounded / index.manager.view）。

        参数：
            order (list[_BuildNode]): 已连边的全部节点。
        返回：
            list[_BuildNode]: 存活节点（保持原创建序）。
        """
        alive: dict[str, _BuildNode] = {n.node_path: n for n in order}
        changed = True
        while changed:
            changed = False
            for n in order:
                nid = n.node_path
                if nid not in alive:
                    continue
                has_child = any(c in alive for c in n.children_ids)
                if n.skeleton or n.content.strip() or n.tables or has_child:
                    continue
                # 空正文叶：删除并从父摘除
                parent = alive.get(n.parent_id) if n.parent_id else None
                if parent is not None and nid in parent.children_ids:
                    parent.children_ids.remove(nid)
                del alive[nid]
                changed = True
        return [n for n in order if n.node_path in alive]

    @staticmethod
    def _attach_ancestors(nodes: list[_BuildNode]) -> None:
        """原地算定祖先链：沿 parent_id 上溯写 ancestor_titles / ancestor_paths（不含自身）。

        祖先链取**真实父链**而非 node_path 号段：中间层级缺节点时按实际父链算
        （5.3 缺 → 5.3.4 的父是 5），也适配无"章/节/条"原生层级的文档。树**深度**与
        节点**种类**（容器/叶）均为纯派生事实，不在此落字段——消费方按 len(ancestor_paths)+1
        取深度、按 children_ids 空否判种类（见 ir.chunk 模块 docstring / index.manager.view）。

        参数：
            nodes (list[_BuildNode]): 已连边（含 parent_id）的节点列表。
        返回：
            无（原地写 ancestor_titles / ancestor_paths）。
        """
        by_path = {n.node_path: n for n in nodes}
        for n in nodes:
            titles: list[str] = []
            paths: list[str] = []
            cur = by_path.get(n.parent_id) if n.parent_id else None
            seen: set[str] = set()
            while cur is not None and cur.node_path not in seen:
                seen.add(cur.node_path)
                titles.append(cur.title)
                paths.append(cur.node_path)
                cur = by_path.get(cur.parent_id) if cur.parent_id else None
            n.ancestor_titles = titles[::-1]
            n.ancestor_paths = paths[::-1]


# ===========================================================================
# §3 切分策略 —— TocSplitter（把 §2 建树收口成 Splitter 契约）
# ===========================================================================


class TocSplitter(Splitter):
    """以 PDF 原生目录为骨架的多层级切分（吃解析层目录打标产物 → TreeBuilder 建树）。"""

    name = "toc"

    def split(self, document: Document, *, max_depth: int | None = None,
              subsplit: str = "none") -> SplitResult:
        """目录打标（§0）→ 建树（§2）→ Chunk 树（含 parent/child + 祖先链 + 引用图）。

        入参 ``document`` 为解析层**格式归一**产物（blocks 为纯版面块、未打标）；本层先 ``CatalogLabeler``
        打标得到带 catalog/catalog_source 的块 + 有序目录条目表（骨架真值），再 ``TreeBuilder`` 建树。
        参数 / 返回：见基类 Splitter.split（max_depth 切目录层级 / subsplit 目录层下按编号细分）；
        ``SplitResult.catalog_blocks`` 顺带返回目录打标快照（调试，catalog_blocks.json）。
        """
        labeler = CatalogLabeler(document.standard_id)
        annotated = labeler.annotate(document.block_dicts())
        chunks = TreeBuilder(max_depth, subsplit).apply(
            annotated,
            entries=labeler.entries,
            std=document.standard_id,
            source_file=document.source_file,
        )
        return SplitResult(chunks=chunks, catalog_blocks=annotated)

    def run_cli(self) -> click.Command:
        """阶段1 实跑入口：返回「解析 → 切分 → 写盘」的 click 命令（只到结构层，不碰表征/索引）。

        与 ``parser.MineruParser.run_cli`` 同构——``splitter.__main__`` 遍历注册表把声明了 ``run_cli``
        的切法挂成 ``python -m splitter <切法名>``（占位切法无 ``run_cli`` → 不出现）。读阶段0 缓存
        ``*_content_list.json``，经 parser 解析（格式归一）成 Document → ``self.split``（目录打标 + 建树）
        → 出 chunks.json / catalog_blocks.json。不依赖 Milvus / 嵌入服务，本地即可跑、专调切分深度
        （max_depth / subsplit）。

        参数：无。
        返回：
            click.Command: 切分命令；由 ``splitter.__main__`` 挂载为 ``python -m splitter toc``。
        """
        split = self.split  # 闭包捕获绑定方法
        default_structured = Path(__file__).resolve().parent.parent / "data" / "structured"

        @click.command(name=self.name)
        @click.option("--input", "input_path", required=True,
                      type=click.Path(exists=True, dir_okay=False, path_type=Path),
                      help="MinerU 输出的 *_content_list.json（阶段 0 缓存）路径。")
        @click.option("--standard-id", default="", help="规范标识；不传则取输入文件名 basename。")
        @click.option("--profile-name", default="default", show_default=True, help="产物子目录名。")
        @click.option("--parser-strategy", default="mineru", show_default=True,
                      help="解析模型（parser/ factory 键；缺省 mineru）。")
        @click.option("--toc-max-depth", type=int, default=None,
                      help="切到第几级目录（号段层级，1=章/2=节/3=条…）；不传=全目录深度。")
        @click.option("--subsplit", type=click.Choice(["none", "number"]),
                      default="none", show_default=True,
                      help="目录层下按编号细分：none=镜像目录不细分 / number=按编号号段再切出更细的编号子节点。")
        @click.option("--structured-dir", type=click.Path(file_okay=False, path_type=Path),
                      default=default_structured, show_default=True, help="结构层产物根目录。")
        @click.option("--preview", is_flag=True, help="只打印前 20 条节点，不写文件。")
        def _cmd(input_path: Path, standard_id: str, profile_name: str, parser_strategy: str,
                 toc_max_depth: int | None, subsplit: str, structured_dir: Path,
                 preview: bool) -> None:
            """只跑切分（阶段 0→1）：解析（格式归一）→ 切分（目录打标 + 建树）→ 出 catalog_blocks.json / chunks.json。"""
            # 延迟引入，避免切分层模块级依赖解析层
            from parser import factory as parser_factory

            standard_id = standard_id or input_path.stem.replace("_content_list", "")
            console.rule(f"[bold]切分（{self.name}）：{standard_id}[/bold]")

            with open(input_path, encoding="utf-8") as f:
                items = json.load(f)
            document = parser_factory.select(parser_strategy).adapt(
                items, standard_id=standard_id, source_file=input_path.name)
            console.print(f"  原始元素：{len(document.blocks)}  "
                          f"toc_max_depth={toc_max_depth} subsplit={subsplit}")

            result = split(document, max_depth=toc_max_depth, subsplit=subsplit)
            chunks = result.chunks
            catalog_blocks = result.catalog_blocks
            print_catalog_stats(catalog_blocks)  # 观测：catalog_source 来源分解
            console.print(f"[green]✓ {len(chunks)} 个 Chunk 节点[/green]")

            if preview:
                console.print("\n[bold]--- 前 20 条节点预览 ---[/bold]")
                for c in chunks[:20]:
                    tables = f"  [{len(c.tables)}表]" if c.tables else ""
                    pg = c.provenance.page[0] if c.provenance.page else 0
                    kind = "container" if c.children_ids else "leaf"
                    lvl = len(c.ancestor_paths) + 1
                    console.print(f"  [cyan]{c.node_path}[/cyan] [{kind}] L{lvl} "
                                  f"(p{pg}) {c.title[:50]}{tables}")
                return

            safe = re.sub(r"[^\w]", "_", standard_id).strip("_")
            out = structured_dir / safe / profile_name
            out.mkdir(parents=True, exist_ok=True)
            _write_json(catalog_blocks, out / "catalog_blocks.json")
            _write_json([c.to_dict() for c in chunks], out / "chunks.json")

        return _cmd
