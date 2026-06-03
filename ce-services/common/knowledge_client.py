"""知识服务 HTTP 客户端 —— 任务层够到检索原语的唯一通道。

任务服务（qa / compliance / 未来算量·审图）不再 ``import retrieval`` 在进程内检索，
而是统一打 ce-code 知识服务（:8100）的原语端点。好处：retrieval + rerank 模型只在
知识服务加载一份，索引预热只一处；任务层保持轻量、可独立部署。

注意行为等价：知识服务 ``/search`` 内部按 ``bm25_top_k = vector_top_k = top_k*2``
调用 ``retrieval.engine.search``，与重构前 orchestration 进程内直调的参数逐字一致，
因此检索结果（含 RRF 合并、引用扩展、强条不截断）保持不变。
"""
from __future__ import annotations

import requests

from common.config import KNOWLEDGE_URL


def search(
    query: str,
    standard: str = "gb50016",
    top_k: int = 15,
    skip_rerank: bool = False,
    base_url: str | None = None,
    timeout: int = 300,
) -> dict:
    """打知识服务 /search，返回完整响应 dict（含 ``clauses`` 与 ``meta``）。

    调用方按需取 ``resp["clauses"]``；HTTP 错误（如未知规范 400 / 索引未就绪 503）
    通过 ``requests.HTTPError`` 上抛，由任务服务转译为对应状态码。
    """
    base = (base_url or KNOWLEDGE_URL).rstrip("/")
    resp = requests.post(
        f"{base}/search",
        json={
            "query": query,
            "standard": standard,
            "top_k": top_k,
            "skip_rerank": skip_rerank,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def expand(
    clause_paths: list[str],
    standard: str = "gb50016",
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打知识服务 /expand，对种子条款做一跳引用图扩展。"""
    base = (base_url or KNOWLEDGE_URL).rstrip("/")
    resp = requests.post(
        f"{base}/expand",
        json={"clause_paths": clause_paths, "standard": standard},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_clause(
    standard: str,
    clause_path: str,
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打知识服务 /clause/{standard}/{path}，按条款号直取单条款。"""
    base = (base_url or KNOWLEDGE_URL).rstrip("/")
    resp = requests.get(f"{base}/clause/{standard}/{clause_path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()
