# 建筑规范 RAG · Docker 一键启动

## 快速开始

```bash
# 1. 准备配置（首次）
cp docker/rag/.env.example docker/rag/.env
# 编辑 .env，至少填好 DATA_DIR（ce-code/data 的绝对路径）

# 2. 首次构建镜像（~5 分钟，torch 镜像较大）
docker compose -f docker/rag/docker-compose.yaml build

# 3. 一键启动
docker compose -f docker/rag/docker-compose.yaml up -d

# 4. 健康检查
curl localhost:8100/health   # knowledge → "service":"retrieval"
curl localhost:8102/health   # qa        → "service":"qa"
curl localhost:8101/health   # compliance→ "service":"compliance"
```

## 三层架构

```
Milvus :19530  vLLM BGE :8097  vLLM Qwen3 :8099  （宿主机，已有）
          ↑              ↑              ↑
    rag-knowledge :8100（GPU，检索原语 /search /expand /clause）
          ↑                    ↑
    rag-qa :8102           rag-compliance :8101
    /qa（检索+生成）         /compliance（合规编排）
```

- `network_mode: host`：容器直接复用宿主机网络，无需额外配置即可访问 Milvus/vLLM
- `knowledge` 先启动并通过健康检查，`qa`/`compliance` 才启动（`depends_on`）

## 常用命令

```bash
# 查看实时日志
docker compose -f docker/rag/docker-compose.yaml logs -f

# 只看某个服务
docker compose -f docker/rag/docker-compose.yaml logs -f knowledge

# 停止
docker compose -f docker/rag/docker-compose.yaml down

# 重新构建（代码有改动时）
docker compose -f docker/rag/docker-compose.yaml build --no-cache knowledge
docker compose -f docker/rag/docker-compose.yaml up -d
```

## 镜像说明

| 镜像 | 基底 | 大小 | GPU |
|---|---|---|---|
| `rag-knowledge` | `pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime` | ~6 GB | ✅（bge-reranker） |
| `rag-tasks` | `python:3.12-slim` | ~200 MB | ❌ |

**首次 `build` 较慢**（需下载 pytorch 基底镜像），之后 `up` 秒级启动。

## 注意事项

- `DATA_DIR` 必须包含已建好的向量索引（`vector_store/GB_50016-20142018/`）
- bge-reranker-large 模型首次加载约需 30 秒，健康检查 `start_period: 30s` 已考虑
- MinerU 等 pipeline 工具**不在此镜像中**，建索引仍需在宿主机用 `uv run`
