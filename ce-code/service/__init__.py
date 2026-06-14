"""服务层（service/）—— 对外知识服务 API + 构建编排（承旧 ``retrieval/server.py`` + 根 ``build.py``）。

  build_service     知识库构建编排（阶段 1→3）：解析 → 切分 → 表征 → 索引。根 build.py 是其薄壳。
  retrieve_service  对外检索编排 + 可观测性（请求级日志/计时/request_id），产响应 dict。
  knowledge_api     FastAPI :8100，检索原语端点（/search /expand /clause /health），契约逐字保持。

下层（parser/splitter/feature/index/retrieval）只产 IR / 原语，service 层做编排 + 传输。
"""
from __future__ import annotations

__all__ = ["build_service", "retrieve_service", "knowledge_api"]
