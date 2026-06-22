"""表格 → 可检索文本（表征层共享 helper）。

造价规范的计量规则（项目编码 / 计量单位 / 工程量计算规则 / 工作内容）多在附录表格里，而 Chunk.content
只存表标题句（"…应按表 A.1.1 的规定执行"），表体不进 content → 召回完全看不到。本 helper 把 ``chunk.tables``
（``[{caption, body:[[表头],[行…]], page}]``）渲染成文本，由 dense/sparse/context_aug 表征注入嵌入/索引文本。

差异化（按消费方语义/长度特性）：dense/context_aug 走向量，**只取 caption**（表主题如"单独土石方"/"措施
项目"——区分性信号）；**不注通用表头**（"项目编码 项目名称 … 工程量计算规则" 各表雷同，注入会让所有表 chunk
向量同质化、并被含"工程量/计算"的查询无差别命中、放大样板噪声）。BM25 无长度惩罚、IDF 自然压低高频词，取
**全表**（caption + 表头 + 全部数据行）供项目编码/计量单位/术语精确召回。
"""
from __future__ import annotations


def render_tables(tables: list[dict], *, full: bool) -> str:
    """把表格列表渲染成可检索文本。

    参数：
        tables —— ``chunk.tables``：``[{caption, body:[[表头],[行…]], page}]``，可空。
        full —— True 取 caption + 表头 + 全部数据行（BM25 精确召回）；False **仅 caption**
            （dense/context_aug 语义召回；通用表头雷同会同质化向量，故不注）。
    返回：
        渲染文本（每段一行：caption；full 时再加表头与各数据行的单元格空格连接）；无表返回空串。
    """
    parts: list[str] = []
    for t in tables or []:
        caption = (t.get("caption") or "").strip()
        if caption:
            parts.append(caption)
        if not full:
            continue  # dense/context_aug：仅 caption（不注通用表头/数据行）
        for row in t.get("body") or []:    # BM25：表头 + 全部数据行
            cells = [str(c).strip() for c in row if str(c).strip()]
            if cells:
                parts.append(" ".join(cells))
    return "\n".join(parts)
