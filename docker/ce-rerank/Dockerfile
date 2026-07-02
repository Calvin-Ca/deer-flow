# CE Rerank 服务镜像（:8095，env CE_RERANK_PORT 可配）—— cross-encoder 精排（bge-reranker-large）
#
# 从知识层 :8100 拆出的**唯一 GPU 消费者**：知识服务检索栈原本进程内加载 FlagReranker，
# 现改 HTTP 调本服务（config.RERANK_URL）。用 pytorch-cuda 基底（torch 预装），只装精排所需。
#
# 构建（从项目根执行）：
#   docker build -f docker/ce-rerank/Dockerfile -t ce-rerank .
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

RUN pip install --no-cache-dir \
    "FlagEmbedding>=1.3" \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "pydantic>=2.7"

WORKDIR /app

# 仅精排服务所需：config.py（读 RERANK_MODEL/DEVICE）+ service 包（rerank_api）。
# service/__init__.py 只有 __all__、不触发子模块导入，故不会拉进 knowledge_api 的重依赖。
COPY ce-code/config.py             config.py
COPY ce-code/service/__init__.py   service/__init__.py
COPY ce-code/service/rerank_api.py service/rerank_api.py

ENV PYTHONUNBUFFERED=1 \
    HF_ENDPOINT=https://hf-mirror.com

EXPOSE 8095
CMD ["python", "-m", "service.rerank_api"]
