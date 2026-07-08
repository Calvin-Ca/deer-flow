# 建筑领域 CE —— ce-rag 服务镜像（:8100，纯 CPU）
#
# 承载：RAG 检索、候选召回与 ce-rag MCP（/mcp）。
# 精排（原唯一 GPU 用途）已拆到 docker/ce-rerank（:8098），本服务经 config.RERANK_URL HTTP 调它 → **纯 CPU**，
# 故基底用 python:slim（不再 pytorch-cuda）。外部依赖（host 网络 localhost 直连）：
#   embedding :8097 / Milvus :19530 / PostgreSQL :5433 / rerank :8098。
#
# 构建（从项目根执行）：
#   docker build -f docker/ce-code/Dockerfile -t rag-knowledge .
FROM python:3.12-slim

# pip 源（默认清华，build 加速；可 --build-arg PIP_INDEX_URL=... 覆盖回官方）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 运行时依赖（service.rag_api 实际 import 链；**不含 torch/FlagEmbedding**——精排已拆出）：
#   pymilvus 向量查询 / rank-bm25 BM25 / psycopg 投影检索(PG) / mcp façade / rich·click（cost 包部分模块 import）
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "pydantic>=2.7" \
    "requests>=2.32" \
    "pymilvus>=2.4.0" \
    "rank-bm25>=0.2.2" \
    "psycopg[binary]>=3.3.4" \
    "mcp>=1.27" \
    "rich>=13" \
    "click>=8"

WORKDIR /app

# 运行时包（service.rag_api import 链；data/ 走 volume 挂载）。
# ingest 只取 ir 子包（Chunk 契约，被 index/feature import）——parser/splitter 是 MinerU build-only、不入镜像。
COPY ce-code/config.py          config.py
COPY ce-code/service/           service/
COPY ce-code/retrieval/         retrieval/
COPY ce-code/index/             index/
COPY ce-code/feature/           feature/
COPY ce-code/cost/              cost/
COPY ce-code/ir/                ir/
COPY ce-code/utils/             utils/
COPY ce-code/ingest/__init__.py ingest/__init__.py
COPY ce-code/ingest/ir/         ingest/ir/

ENV PYTHONUNBUFFERED=1

EXPOSE 8100
# 模块式启动（WORKDIR=/app 在 sys.path，`import config` / `service.rag_api` 均可解析）
CMD ["python", "-m", "service.rag_api"]
