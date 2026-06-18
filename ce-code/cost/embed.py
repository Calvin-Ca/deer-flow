"""cost/embed.py —— 嵌入服务调用工具（造价轨自持，不依赖规范 RAG 栈）。

bill_index（建库）与 bill_match（召回）共用：调 vLLM embedding API（复用规范轨已部署的
bge-large-zh-v1.5 @ embed_url, dim 1024）把文本批量嵌成向量。原在 ``index/vector_index.py``，
随规范条文检索 RAG 移除而抽到造价轨内（造价知识库自持嵌入工具）。
"""
from __future__ import annotations


def embed_texts(texts: list[str], embed_url: str, model_id: str, batch_size: int) -> list[list[float]]:
    """调 vLLM embedding API，分批返回向量列表（空串占位、超长截断 480 字）。

    参数：
        texts —— 待嵌入文本列表。
        embed_url —— 嵌入服务地址（如 http://localhost:8097）。
        model_id —— 模型 id（vLLM 部署名）。
        batch_size —— 每批条数。
    返回：与 texts 同序的向量列表（list[list[float]]）。
    """
    import requests

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        batch = [t[:480] if t.strip() else "无内容" for t in batch]
        resp = requests.post(
            f"{embed_url}/v1/embeddings",
            json={"model": model_id, "input": batch},
            timeout=120,
        )
        if not resp.ok:
            print(f"[ERROR] batch {i}: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda x: x["index"])
        all_embeddings.extend(item["embedding"] for item in data)
    return all_embeddings
