# 建筑规范 RAG —— 任务服务镜像（qa + compliance 共进程，:8101）
#
# 纯 HTTP 客户端，不依赖 torch/Milvus，镜像极轻量。
#
# 构建（从项目根执行）：
#   docker build -f docker/ce-services/Dockerfile -t rag-tasks .
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "requests>=2.32" \
    "pydantic>=2.7"

WORKDIR /app

COPY ce-services/main.py       main.py
COPY ce-services/common/       common/
COPY ce-services/qa/           qa/
COPY ce-services/compliance/   compliance/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8101"]
