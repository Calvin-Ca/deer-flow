"""服务层（service/）—— ce-rag / ce-db 对外 API。

  retrieve_service  对外检索编排 + 可观测性（请求级日志/计时/request_id），产响应 dict。
  rag_api           FastAPI :8100，ce-rag 检索、候选召回与 MCP。
  db_api            FastAPI :8102，ce-db 结构化真值、价格、定额、费率与 MCP。

知识库构建不在本层：build 是**本地命令行**（根 ``build.py``，一条命令跑完 解析→切分→表征→索引），
不作为服务。下层（parser/splitter/feature/index/retrieval）只产 IR / 原语，service 层做编排 + 传输。
"""
from __future__ import annotations

__all__ = ["retrieve_service", "rag_api", "db_api"]
