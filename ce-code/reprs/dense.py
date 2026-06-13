"""dense 表征 —— 语义召回的待嵌入文本（免费产文本；向量由索引期 04 计算）。

PRD §3.1：dense 载体=向量，作用=语义召回，成本=embed。**职责切分**：表征层（本模块）
只产「该嵌入哪段文本」（title + content 的干净正文），**向量本身在索引期由
04_build_index 用 embedding 模型统一计算**——embedding/rerank 模型「只加载一份、唯一
owner」在检索栈（PRD §一），表征层不加载模型、不联网，故仍属「免费」文本投影。

与 context_aug 区别：dense 只嵌节点自身正文（纯语义），context_aug 嵌拼了祖先链的
增强文本（语境/ small-to-big 入口）；两者是两路向量通道。
"""
from __future__ import annotations

KIND = "dense"


def build(node: dict) -> dict:
    """产 dense 表征：待嵌入文本 = title + content（向量留索引期填）。

    参数：
        node (dict): schema.Node。
    返回：
        dict: schema.Representation —— {kind, text}（vector 字段由 04 索引期补）。
    """
    title = node.get("title", "").strip()
    content = node.get("content", "").strip()
    text = f"{title}\n{content}".strip() if content else title
    return {"kind": KIND, "text": text}
