"""知识服务 HTTP 客户端 —— 任务层够到规范条文检索原语的唯一通道。

任务层 Norm-QA（造价规范问答）不 ``import retrieval`` 在进程内检索，而是打 ce-code RAG 服务
（默认 :8100）的检索原语 ``/search/clause`` ``/expand/clauses`` ``/clause/{standard}/{path}``。好处：retrieval +
rerank 模型只在知识服务加载一份、索引预热只一处；任务层保持轻量、可独立部署。

与造价取数客户端 ``common.cost_client`` 并列：本模块取 ce-rag 规范条文（Norm-QA），
cost_client 分别调用 ce-rag 候选召回与 ce-db 结构化取数（CostAgent）。

``standard`` 参数取 ``config.STANDARD_ALIASES`` 的代号（造价规范如 ``gb50854-2024`` /
``gb50500-2013``，按国标版本隔离），Norm-QA 调用须显式传，不依赖默认。
"""
from __future__ import annotations

import requests

from common.config import RAG_URL


def search(
    query: str,
    standard: str = "gb50016",
    top_k: int = 15,
    skip_rerank: bool = False,
    base_url: str | None = None,
    timeout: int = 300,
) -> dict:
    """打 RAG 服务 /search/clause，返回完整响应 dict（含 ``evidence`` 与 ``meta``）。

    调用方按需取 ``resp["clauses"]``；HTTP 错误（如未知规范 400 / 索引未就绪 503）
    通过 ``requests.HTTPError`` 上抛，由任务服务转译为对应状态码。
    """
    base = (base_url or RAG_URL).rstrip("/")
    resp = requests.post(
        f"{base}/search/clause",
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
    node_paths: list[str],
    standard: str = "gb50016",
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打 RAG 服务 /expand/clauses，对种子条款做一跳引用图扩展。"""
    base = (base_url or RAG_URL).rstrip("/")
    resp = requests.post(
        f"{base}/expand/clauses",
        json={"node_paths": node_paths, "standard": standard},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_clause(
    standard: str,
    node_path: str,
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打 RAG 服务 /clause/{standard}/{path}，按条款号直取单条款。"""
    base = (base_url or RAG_URL).rstrip("/")
    resp = requests.get(f"{base}/clause/{standard}/{node_path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()
