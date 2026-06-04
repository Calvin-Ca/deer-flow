# 建筑规范 RAG —— 任务服务镜像（qa :8102 / compliance :8101）
#
# 纯 HTTP 客户端，不依赖 torch/Milvus，镜像极轻量。
# qa 和 compliance 共用同一镜像，通过 docker-compose CMD 区分。
#
# 构建（从项目根执行）：
#   docker build -f docker/rag/Dockerfile.tasks -t rag-tasks .
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "requests>=2.32" \
    "pydantic>=2.7"

WORKDIR /app

COPY ce-services/common/     common/
COPY ce-services/qa/         qa/
COPY ce-services/compliance/ compliance/

ENV PYTHONUNBUFFERED=1

# CMD 在 docker-compose 中按服务覆盖
