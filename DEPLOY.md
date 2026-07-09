# 部署与启动手册（全服务 · dev / prod）

> 本文是**所有服务 + 依赖的启动单一入口**。服务器生产的细节（IP、sudo/rootless 三坑、重建缓存）见
> [`docker/README.md`](docker/README.md)；dev 两态调试约定见 [`CLAUDE.md §2.3`](CLAUDE.md)。三处若冲突以本表 + docker/README 为准。

---

## 1. 服务全景（4 层）

| 层 | 服务 | 端口 | 起法归属 | 说明 |
|---|---|---|---|---|
| **① 依赖设施** | PostgreSQL（ce_cost） | 5433 | deps compose（rootless） | ce-db 的结构化真值库 |
| | Milvus(+etcd/minio) | 19530 | deps compose（rootless） | ce-rag 向量库 |
| | embed（bge-large） | 8097 | **外部 GPU**·先起 | 向量化 |
| | vLLM Qwen3-8B | 8099 | **外部 GPU**·先起 | agent 默认 LLM |
| | rerank（bge-reranker） | 8095 | ce-rerank compose（GPU，sudo） | ce-rag 精排；不起则降级 RRF |
| **② 知识层** | ce-rag（检索 MCP） | 8100 | ce-code compose **或** 裸机 | 条文/清单候选/证据检索 |
| | ce-db（结构化取数 MCP） | 8102 | ce-code compose **或** 裸机 | 定额/工料机/价格/费率取数 |
| **③ harness** | gateway | 8001 | serve.sh / deploy.sh / docker.sh | 内嵌 lead_agent + `cost_workflow_*` |
| | frontend | 3000 | 同上 | Next.js |
| | nginx 入口 | 2026 | 同上（仅 Docker 态发布） | 统一反代入口 |
| **④ 观测（可选）** | Prometheus / Grafana | 19090 / 3001 | ce-monitoring compose | |
| | Langfuse | 3030 | ce-langfuse compose（rootless） | agent trace |

**启动顺序（依赖在前）**：① 依赖设施 → ② 知识层 → ③ harness →（④ 观测随时）。

**心智模型**：①② 依赖是"起一次留着"的底座（dev/prod 共用）；真正在 dev↔prod 之间切的只有 **③ harness**。

> **组价编排在 harness 内**（`backend/app/ce/cost/` 的 `cost_workflow_*` + `ce-rag`/`ce-db` MCP）。
> 原 `ce-services` 任务层（:8101）已整体退役，**不在任何启动流里**。

---

## 2. 生产态（全 Docker，服务器）

```bash
# ① 依赖：数据层（rootless，无 sudo）+ GPU 外部服务（embed/vLLM 先自行起）
docker compose -f docker/ce-code/docker-compose.deps.yaml up -d          # PG :5433 + Milvus :19530
# ② 精排（sudo GPU）
sudo docker compose -f docker/ce-rerank/docker-compose.yaml --env-file docker/ce-rerank/.env up -d
# ③ 知识层 ce-rag :8100 + ce-db :8102（sudo host-net，一条起两个）
sudo docker compose -f docker/ce-code/docker-compose.yaml --env-file docker/ce-code/.env up -d
#    只起一个：… up -d ce-db   /   … up -d ce-rag   （两服务无 depends_on，可单独起）
# ④ harness（sudo；首次先把 config.yaml 的 vLLM base_url 改成 host.docker.internal:8099）
sed -i 's#http://localhost:8099/v1#http://host.docker.internal:8099/v1#' config.yaml
sudo ./scripts/deploy.sh                     # = make up；nginx :2026 对外
# ⑤ 观测（可选）
sudo docker compose -f docker/ce-monitoring/docker-compose.yaml --env-file docker/ce-monitoring/.env up -d
docker compose -f docker/ce-langfuse/docker-compose.yaml up -d
```

- 入口：`http://<服务器IP>:2026`。
- 停：`sudo ./scripts/deploy.sh down`（harness）、各 `compose down`（**依赖数据层谨慎，别 `-v`**）。
- 动过后端源码重建须防 COPY 缓存坑：`sudo ./scripts/deploy.sh build --no-cache gateway` + `up -d --force-recreate gateway`（详见 `docker/README.md §5`）。

---

## 3. 开发态（harness 裸机热更/断点，依赖仍用底座）

**前提**：依赖设施 ①（PG/Milvus/embed/vLLM/rerank）已在跑（通常仍是 Docker，起一次留着）。先停 Docker harness 腾 :2026：`sudo ./scripts/deploy.sh down`。

**知识层 ②（裸机，二选一起法）**：
```bash
cd ce-code && uv run python -m service.rag_api     # ce-rag :8100
cd ce-code && uv run python -m service.db_api      # ce-db  :8102
```

**harness ③（dev 热更 / 断点）**：
```bash
# config.yaml vLLM base_url 翻回 localhost:8099（两态唯一要来回翻的配置）
sed -i 's#host.docker.internal:8099#localhost:8099#' config.yaml
# 方式 A：一键（gateway 无 reload + 前端热更）
make dev                                            # = scripts/serve.sh --dev
# 方式 B：断点调试（推荐）——VS Code debugpy 起 gateway :8001
#   launch.json 必带 "envFile": "${workspaceFolder}/.env"（灰度开关在 .env，缺了等于白验）；禁加 --reload
#   前端：cd frontend && pnpm exec next dev --turbo --port 2026
```

**CE 内网试用一键裸机**（ce-rag + ce-db + gateway + frontend 一起，生产模式前端）：
```bash
scripts/ce-serve.sh            # 起四服务 + 逐个 /health 自检；--build 先 pnpm build；--stop 全停
```

**Docker-dev 态**（容器里跑 harness、带热更挂载）：`make docker-start`（= `scripts/docker.sh start`，用 `docker-compose-dev.yaml`）。

---

## 4. 关键点

1. **`ce-rag`/`ce-db` 是 HTTP-MCP，起法自由**——裸机 `uv run` / Docker / systemd 三选一，agent 侧 `extensions_config.json` 指向 :8100/:8102 不变（MCP 是接口，不规定部署方式）。
2. **唯一要来回翻的配置**：`config.yaml` 的 vLLM `base_url`（Docker 态 `host.docker.internal:8099` ↔ dev 态 `localhost:8099`）。后端连不上 8099 先查这里。
3. **sudo 边界**：GPU / app / 监控容器走系统 daemon（`sudo docker`）；deps 数据层 + Langfuse 是 rootless（**不加 sudo**）。混用会建影子容器。
4. **健康自检（宿主机）**：
   ```bash
   for p in 8100 8102 8095 8097 8099; do printf "$p -> "; curl -s -o /dev/null -w "%{http_code}\n" localhost:$p/health 2>/dev/null || echo down; done
   curl -sI localhost:2026        # 307=nginx 活
   ```
5. **dev 态验不了的四边界**（回生产前过一次）：① 前端 `pnpm typecheck`（next dev 不跑 tsc）；② 多 worker（生产 `--workers 4`）；③ Dockerfile/compose/nginx 类改动；④ `uv add` 新依赖须重建镜像。
