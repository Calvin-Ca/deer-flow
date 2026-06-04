# 建筑规范 RAG —— 知识服务镜像（:8100）
#
# 用 pytorch 官方镜像作基底，torch 已预装，避免重复下载 ~2GB wheel。
# 只安装知识服务运行所需的依赖（不含 MinerU 等 pipeline 工具）。
#
# 构建（从项目根执行）：
#   docker build -f docker/rag/Dockerfile.knowledge -t rag-knowledge .
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

RUN pip install --no-cache-dir \
    "pymilvus>=2.4.0" \
    "rank-bm25>=0.2.2" \
    "requests>=2.32" \
    "FlagEmbedding>=1.3" \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "pydantic>=2.7"

WORKDIR /app

# 只拷送服务所需的源码，data/ 通过 volume 挂载
COPY ce-code/retrieval/ retrieval/
COPY ce-code/service/   service/

ENV PYTHONUNBUFFERED=1 \
    HF_ENDPOINT=https://hf-mirror.com

EXPOSE 8100
CMD ["python", "service/server.py"]
