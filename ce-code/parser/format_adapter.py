"""格式适配器 — FormatAdapter（解析层·阶段 0→1 衔接：MinerU v1 → 统一块 schema）。

纯格式转换、无结构语义：把 MinerU v1 `content_list.json` 的扁平元素归一成下游各层
共用的统一块 schema（page 归一、HTML 表格解析成矩形二维表、text_level 原样透传、
block_idx 溯源）。不解析条文号 / 不打目录标签 / 不建树——那些是切分层 `splitter/`
（目录打标器 catalog_labeler + 建树器 tree_builder）的事。

归属 parser/（切分前的通用适配，不内聚进某一切法，可被任意 splitter 复用；可独立单测）。
本模块纯 stdlib（html.parser），无项目内跨层依赖。

输入：MinerU v1 `content_list.json` 反序列化出的 list[dict]。
输出：统一块列表 list[dict]（见 FormatAdapter.adapt 文档）。
"""

from __future__ import annotations

from html.parser import HTMLParser


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


class FormatAdapter:
    """MinerU v1 格式适配器：纯格式转换，无结构语义。

    功能：
        page_number 跳过；page 归一（0-base → 1-base）；HTML 表格解析；
        text_level 原样透传（MinerU 标题层级，仅标题块有此键）；
        list 条目保留整体（不展开、不做目录过滤，由目录打标器处理）。

    参数：无（调用静态方法 adapt(items) 传入原始列表）。
    返回：
        adapt(items) 返回 list[dict]，schema：
            type       元素类型（text / list / table / equation / footer）
            text       文本内容（list 为空字符串，table 为 caption）
            page       页码（从 1 起）
            text_level MinerU 标题层级（原样透传；仅标题块有此键，缺则非标题）
            block_idx  原始 content_list 中的下标（供节点 provenance 溯源回 data/parsed）
            list_items list 条目列表（仅 list）
            body       矩形二维表体（仅 table）
            img_path   裁切图路径（仅 table）
    """

    @staticmethod
    def adapt(items: list[dict]) -> list[dict]:
        """MinerU v1 原始列表 → 统一元素列表。

        参数：
            items (list[dict]): MinerU v1 输出的扁平元素 list。
        返回：
            list[dict]: 统一元素列表。
        """
        out: list[dict] = []
        for idx, it in enumerate(items):
            t = it.get("type", "text")
            page = it.get("page_idx", 0) + 1

            if t == "page_number":
                continue

            if t == "table":
                parts = it.get("table_caption") or []
                caption = " ".join(s.strip() for s in parts if isinstance(s, str) and s.strip())
                out.append({
                    "type": "table",
                    "text": caption,
                    "page": page,
                    "block_idx": idx,
                    "body": _html_table_to_rows(it.get("table_body", "")),
                    "img_path": it.get("img_path", ""),
                })
                continue

            if t == "list":
                list_items = [s.strip() for s in (it.get("list_items") or []) if s.strip()]
                if list_items:
                    out.append({
                        "type": "list",
                        "text": "",
                        "page": page,
                        "block_idx": idx,
                        "list_items": list_items,
                    })
                continue

            text = it.get("text", "").strip()
            if not text:
                continue
            elem = {
                "type": t,
                "text": text,
                "page": page,
                "block_idx": idx,
            }
            if "text_level" in it:  # MinerU 标题层级：原样透传，仅标题块有此键
                elem["text_level"] = it["text_level"]
            out.append(elem)
        return out
