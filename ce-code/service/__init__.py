"""服务层（service/）—— 对外知识服务 API（承旧 ``retrieval/server.py``）。

  retrieve_service  对外检索编排 + 可观测性（请求级日志/计时/request_id），产响应 dict。
  knowledge_api     FastAPI :8100，检索原语端点（/search /expand /clause /health），契约逐字保持。
  rag_api/db_api    新拆分入口：ce-rag / ce-db（REST + MCP）。

知识库构建不在本层：build 是**本地命令行**（根 ``build.py``，一条命令跑完 解析→切分→表征→索引），
不作为服务。下层（parser/splitter/feature/index/retrieval）只产 IR / 原语，service 层做编排 + 传输。
"""
from __future__ import annotations

__all__ = ["retrieve_service", "knowledge_api", "rag_api", "db_api"]
