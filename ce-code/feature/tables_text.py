"""表格 → 可检索文本（表征层共享 helper）。

造价规范的计量规则（项目编码 / 计量单位 / 工程量计算规则 / 工作内容）多在附录表格里，而 Chunk.content
只存表标题句（"…应按表 A.1.1 的规定执行"），表体不进 content → 召回完全看不到。本 helper 把 ``chunk.tables``
（``[{caption, body:[[表头],[行…]], page}]``）渲染成文本，由 dense/sparse/context_aug 表征注入嵌入/索引文本。

差异化（按消费方长度约束）：dense/context_aug 走向量、bge max ~512 token，**只取 caption + 表头**（语义信号
紧凑、避免长表截断稀释向量）；BM25 无长度惩罚，取**全表**（caption + 表头 + 全部数据行）供项目编码/术语精确召回。
"""
from __future__ import annotations


def render_tables(tables: list[dict], *, full: bool) -> str:
    """把表格列表渲染成可检索文本。

    参数：
        tables —— ``chunk.tables``：``[{caption, body:[[表头],[行…]], page}]``，可空。
        full —— True 取 caption + 表头 + 全部数据行（BM25 精确召回）；False 仅 caption + 表头
            （dense/context_aug 语义召回，避免长表撑爆向量上下文）。
    返回：
        渲染文本（每段一行：caption、表头、各数据行的单元格以空格连接）；无表返回空串。
    """
    parts: list[str] = []
    for t in tables or []:
        caption = (t.get("caption") or "").strip()
        if caption:
            parts.append(caption)
        body = t.get("body") or []
        if not body:
            continue
        header, rows = body[0], (body[1:] if full else [])
        header_cells = [str(c).strip() for c in header if str(c).strip()]
        if header_cells:
            parts.append(" ".join(header_cells))
        for row in rows:
            cells = [str(c).strip() for c in row if str(c).strip()]
            if cells:
                parts.append(" ".join(cells))
    return "\n".join(parts)
