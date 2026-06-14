"""Document IR —— 解析层（parser/）产物 = 一篇文档的统一元素流（结构层上游）。

替代旧「``FormatAdapter.adapt`` 返回 list[dict]」为显式 ``@dataclass``：
``Document`` = 规范标识 + 溯源文件 + **有序 Block 列表**；``Block`` = 一个统一元素
（MinerU 等任意解析模型归一后的最小单位）。切分层（splitter/）吃 ``Document.blocks``
建树，**不关心上游用的是哪种解析模型**——这是「多解析模型可插拔」的边界。

Block 字段与 MinerU v1 适配产物对齐（见 parser/format_adapter.py）：纯版面/内容事实，
**不含结构语义**（条文号 / 目录标签 / 树边在切分层算）。``text_level`` 为 None 即非标题。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Block:
    """一个统一元素（解析模型归一后的最小单位）。

    字段：
        type        元素类型（text / list / table / equation / footer / image …）。
        text        文本内容（list 为空串，table 为 caption）。
        page        页码（1-base）。
        block_idx   原始解析产物中的下标（供 Chunk.provenance 溯源回阶段 0 缓存）。
        text_level  标题层级（仅标题块有；None = 非标题）。**只用其有无判是否标题**，
                    层级数值不取（真正层级由切分层按号段算）。
        list_items  list 条目（仅 type==list）。
        body        矩形二维表体（仅 type==table；已展开 colspan/rowspan）。
        img_path    裁切图路径（仅 table / image）。
    """

    type: str
    text: str = ""
    page: int = 0
    block_idx: int = -1
    text_level: int | None = None
    list_items: list[str] = field(default_factory=list)
    body: list[list[str]] = field(default_factory=list)
    img_path: str = ""

    def to_dict(self) -> dict:
        """切分层内部消费的 block dict（与旧 FormatAdapter 输出键一致；省略空值）。

        ``text_level`` 仅在标题块写键（与旧「仅标题块有此键」语义一致，
        消费方靠 ``"text_level" in block`` 判是否标题）。
        """
        out: dict = {"type": self.type, "text": self.text,
                     "page": self.page, "block_idx": self.block_idx}
        if self.text_level is not None:
            out["text_level"] = self.text_level
        if self.list_items:
            out["list_items"] = self.list_items
        if self.body:
            out["body"] = self.body
        if self.img_path:
            out["img_path"] = self.img_path
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(
            type=d.get("type", "text"),
            text=d.get("text", ""),
            page=int(d.get("page", 0)),
            block_idx=int(d.get("block_idx", -1)),
            text_level=d.get("text_level"),
            list_items=list(d.get("list_items") or []),
            body=list(d.get("body") or []),
            img_path=d.get("img_path", ""),
        )


@dataclass
class Document:
    """一篇文档的解析产物（标识 + 溯源 + 有序 Block 列表）。

    字段：
        standard_id 规范唯一标识（逐块继承到 Chunk）。
        source_file 原始解析产物路径（相对 data/parsed/），写入 Chunk.provenance。
        blocks      有序统一元素列表。
    """

    standard_id: str = ""
    source_file: str = ""
    blocks: list[Block] = field(default_factory=list)

    def block_dicts(self) -> list[dict]:
        """切分层内部消费格式：list[block dict]（复用既有 CatalogLabeler/TreeBuilder 的 dict 管道）。"""
        return [b.to_dict() for b in self.blocks]

    def to_dict(self) -> dict:
        return {
            "standard_id": self.standard_id,
            "source_file": self.source_file,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        return cls(
            standard_id=d.get("standard_id", ""),
            source_file=d.get("source_file", ""),
            blocks=[Block.from_dict(b) for b in (d.get("blocks") or [])],
        )
