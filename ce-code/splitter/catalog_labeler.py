"""目录打标器 — CatalogLabeler（结构层·阶段 1 标注 = 目录打标 + 目录定位）。

依据文档**目录页**这一结构真值，给每个解析块打一个 `catalog` 标签——

  - 块本身就是「文档前面的目录页」 → ``catalog = "toc"``；
  - 否则 → ``catalog = 它所属目录条目的标题``（「属于目录里哪一条」），
    目录里没有的块（封面/前言之前等）→ ``catalog = None``。

定位用**方案 5（混合）**：以目录页解析出的有序条目表为骨架（方案 2 目录锚定），
正文按文档序顺序扫描、块文本与目录条目归一化匹配来确认标题边界（方案 1 编号/标题
对齐），其后每块归属「最近命中的目录条目」。目录只列到节（x.x）时，条（x.x.x）等
更深块自然归属其所在节的条目——这正是「属于目录里哪一条」。无目录页时退化为以
MinerU 标题块自身标题作边界（best-effort）。

每块再带一个 `catalog_source` 审计字段，记录该 `catalog` 由方案 5 哪条子机制得来
（让混合方法在结果里可见、可统计），取值：

  - ``toc_page``         块本身是目录页（区域判据命中）       —— 方案2·目录识别
  - ``toc_match``        正文块命中目录条目、确认为标题边界   —— 方案2锚定 + 方案1对齐
  - ``inherited``        继承最近命中条目（条归节 / 成员块）  —— 归属传播
  - ``heading_fallback`` 无目录，按 text_level 标题块自身标题定边界 —— 方案1·退化路径
  - ``front_matter``     封面 / 扉页 / 前言等正文前置内容（首个目录条目命中前，catalog=None）

设计约束（承 2026-06-12 职责重划）：本层只产「目录归属」这一可靠标签（catalog +
catalog_source 审计），**不**解析条文号 / node_type / 层级 / 建树——那些是建树器
TreeBuilder（tree_builder.py）由 clause_path 号段数算定的「固有事实」。**对 MinerU 结果不做减法**：
目录页块只标 ``catalog="toc"`` 保留（建树阶段据此不并入正文），目录判定采**区域**
判据（连续成行 / 整列），不据单行启发式丢正文块。

归属（2026-06-13 迁入新框架）：本模块是切分策略 ``TocSplitter``（splitter/toc.py）的
内部实现件，与建树器 ``tree_builder.py`` 同在 splitter/ 包内；与建树两关注点解耦、可独立
单测。本模块纯 stdlib + rich，无项目内跨层依赖。

输入：FormatAdapter.adapt() 产出的统一元素列表。
输出：每块带 standard_id + catalog + catalog_source 的扁平列表（不建树、不丢块）。
      text_level 由 MinerU 经 FormatAdapter 原样透传（仅标题块有此键；本层只读用于匹配，不改）。
      list 块展平后不再保留 list_items；raw 已删（需 MinerU 原件靠 block_idx 回查 data/parsed）。
"""

from __future__ import annotations

import re
from collections import Counter

from rich.console import Console
from rich.table import Table

console = Console()

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
    """归一化文本用于匹配：全角数字/点转半角、去所有空白（含全角空格）。

    参数：
        text (str): 原始文本。
    返回：
        str: 归一化串（仅供内部匹配，不回写块）。
    """
    return re.sub(r"\s+", "", text.translate(_FULLWIDTH_DIGITS))


def _split_catalog_line(text: str) -> tuple[str, int | None]:
    """目录条目行 → (干净标题, 页码)。剥掉尾部页码引用与导引点。

    参数：
        text (str): 一条目录行，如 "5.3 防火分区 …… 23" / "1 总则（1）"。
    返回：
        tuple[str, int | None]: (标题, 页码)；无尾页码时页码为 None、标题为整行去空白。
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
    """目录页列表判定：过半条目以「页码」结尾则整列为目录页（强信号，整列盖章）。

    功能：识别 MinerU 把整个目录解析成单个 list 元素的情形。**不做减法**——据此把
        整列条目标 ``catalog="toc"`` 保留（含无尾页码的章名续行）。

    参数：
        items (list[str]): list 元素的 list_items。
        thresh (float): 命中比例阈值，默认 0.5。
    返回：
        bool: True = 整列为目录页。
    """
    if not items:
        return False
    hits = sum(1 for x in items if _CATALOG_TAIL_RE.search(x.strip()))
    return hits >= max(1, len(items)) * thresh


class CatalogLabeler:
    """目录打标器：给每块打 `catalog`（"toc" / 所属目录条目标题 / None）。

    功能（方案 5 混合）：
        ① 展平 + 盖章 standard_id（list 条目逐条展开，不丢块）；
        ② 目录页识别（区域判据）：整列目录（_is_catalog_list）或连续成行的
           entry_like 文本（run ≥ MIN_TOC_RUN）→ catalog="toc"；
        ③ 解析目录条目表（有序，骨架真值）；
        ④ 目录定位：正文按文档序顺序扫描，归一化匹配目录条目切换「当前条目」，
           每块 catalog = 最近命中条目标题（条等深层块归属其节的条目）；无目录页时
           退化为以 text_level 标题块自身标题作边界。每块同时记 catalog_source 审计该
           catalog 由哪条子机制得来（toc_page/toc_match/inherited/heading_fallback/front_matter）。
        text_level（MinerU 标题层级）由 FormatAdapter 原样透传，本层只读用于匹配、不重打，
        不解析条文号/层级（那是建树层的事）。

    参数：
        standard_id (str): 规范唯一标识，逐块盖章。
    返回：
        调用 annotate(elements) 返回 list[dict]，每块带 standard_id + catalog。
    """

    def __init__(self, standard_id: str) -> None:
        """初始化目录打标器。

        参数：
            standard_id (str): 规范唯一标识。
        返回：
            无。
        """
        self.standard_id = standard_id
        self.entries: list[dict] = []  # annotate() 后填：有序目录条目表（骨架真值，供建树器取）

    def annotate(self, elements: list[dict]) -> list[dict]:
        """展平 → 目录打标 → 目录定位，逐块写 standard_id + catalog。

        副作用：把解析出的有序目录条目表存到 ``self.entries``（``{title, norm, page}``，
        骨架真值），供建树器 TreeBuilder 以目录条目为骨架建树（无目录页时为空列表）。

        参数：
            elements (list[dict]): FormatAdapter.adapt() 产出的统一元素列表。
        返回：
            list[dict]: 每块带 standard_id + catalog 的扁平列表（不建树、不丢块）。
        """
        blocks = self._flatten(elements)
        self._mark_toc(blocks)
        self.entries = self._parse_entries(blocks)
        self._locate(blocks, self.entries)
        return blocks

    def _flatten(self, elements: list[dict]) -> list[dict]:
        """展平为块列表，盖章 standard_id；整列目录的 list 条目直接标 catalog="toc"。

        list 块逐条展开成块（展开后丢弃 list_items 字段，不逐块冗余保留）。

        参数：
            elements (list[dict]): FormatAdapter 元素列表。
        返回：
            list[dict]: 块列表（list 已逐条展开；每块含 standard_id，catalog 暂未补全）。
        """
        out: list[dict] = []
        for elem in elements:
            base = {**elem, "standard_id": self.standard_id}
            if elem["type"] == "list":
                items = base.pop("list_items", [])  # 展开后不再逐块保留 list_items（去冗余）
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
        """单块是否「像一条目录行」：text/list 短行且行尾带页码引用。

        参数：
            block (dict): 展平后的块。
        返回：
            bool: True = 形似目录条目（仅作区域判据的逐块信号）。
        """
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

        功能：只把**成片**的目录行判为目录，孤立的「行尾带数字」正文短行不误判、不丢弃
            （承「不做减法」）。整列目录已在 _flatten 直接盖章，本步补「逐行排版的目录」。

        参数：
            blocks (list[dict]): _flatten 产出的块列表（原地写 catalog）。
        返回：
            无。
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
        """从 catalog="toc" 的块解析有序目录条目表（骨架真值）。

        参数：
            blocks (list[dict]): 已标目录的块列表（文档序）。
        返回：
            list[dict]: 条目列表，每条 {title, norm, page}；norm 供定位匹配，page 供调试。
        """
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
        """目录定位：给每块写 catalog（所属条目标题）+ catalog_source（来源审计，方案 5）。

        功能：
            目录页块——catalog 已为 "toc"，catalog_source = toc_page。
            有目录条目时——维护单调前瞻指针，正文块归一化文本命中 entries[ptr:ptr+W]
            中某条（精确相等，或 text_level 标题块前缀相等）即切换「当前条目」、指针前移，
            该块 source = toc_match；未命中则 catalog = 当前条目（条等深层块自然归属其
            节），source = inherited（首个命中前 catalog=None、source=front_matter）。
            无目录条目时——退化：text_level 标题块自身标题作边界（source=heading_fallback），
            其余继承（source=inherited / front_matter）。

        参数：
            blocks (list[dict]): 块列表（文档序，原地写 catalog / catalog_source）。
            entries (list[dict]): _parse_entries 产出的有序条目表。
        返回：
            无。
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

    def print_stats(self, annotated: list[dict]) -> None:
        """打印目录打标统计：标题数 + 按 catalog_source 分解各来源占比（方案 5 可见）。

        参数：
            annotated (list[dict]): annotate() 产出的标注列表。
        返回：
            无（打印到终端）。
        """
        total = len(annotated)
        headings = sum(1 for e in annotated if e.get("text_level") is not None)
        src = Counter(e.get("catalog_source") for e in annotated)
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
