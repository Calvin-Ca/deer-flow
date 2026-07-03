# CE 生产部署（服务器 172.19.3.136）

> 拓扑决策与 rootless 三个坑详见记忆 `project_ce_deploy_topology`；本文是操作手册。

---

## 1. 拓扑一览

> `curl` 均省 `-s`；`起法` / `验证` / 日志的完整命令见表下。

| 分层 | 服务 | 端口 | daemon | 起法 | 验证 | 容器名 |
|---|---|---|---|---|---|---|
| **服务层** | nginx 入口 | **2026** | sudo bridge | deploy.sh | `curl -sI :2026`（307=活）| deer-flow-nginx |
|  | gateway | 8001 | sudo bridge | deploy.sh | 见注 ① | deer-flow-gateway |
|  | 前端 | 内3000 | sudo bridge | deploy.sh | 经 nginx :2026 | deer-flow-frontend |
|  | 知识 | **8100** | sudo host-net | ce-services | `curl :8100/health` | rag-knowledge |
|  | 任务 | **8101** | sudo host-net | ce-services | `curl :8101/health` | rag-tasks |
| **基础设施** | 精排 rerank | 8095 | sudo GPU | ce-rerank | `curl :8095/health` | ce-rerank |
|  | embed | 8097 | sudo GPU | 外部·先起 | `curl :8097/v1/models` | vllm-bge-large |
|  | vLLM 8B（默认）| 8099 | sudo GPU | 外部·先起 | `curl :8099/v1/models` | vllm-qwen3-8b |
|  | vLLM 32B（172.19.2.2）| 8001 | 另一台机 | 外部 | `curl 172.19.2.2:8001/v1/models` | （在该机）|
|  | PostgreSQL | 5433 | rootless | deps | `pg_isready -p 5433` | ce-postgres |
|  | Milvus | 19530 | rootless | deps | `curl :9091/healthz` | ce-milvus |
| **监控/观测** | Prometheus | 19090 | sudo host-net | ce-monitoring | `curl :19090/-/healthy` | ce-prometheus |
|  | Grafana | 3001 | sudo host-net | ce-monitoring | `curl :3001/api/health` | ce-grafana |
|  | Langfuse | 3030 | rootless | ce-langfuse | `curl -sI :3030` | langfuse-langfuse-web-1 |

**起法**：`deploy.sh`=`sudo ./scripts/deploy.sh`；`ce-*`=`sudo docker compose -f docker/ce-*/docker-compose.yaml --env-file docker/ce-*/.env up -d`；`deps`=`docker compose -f docker/ce-code/docker-compose.deps.yaml up -d`（rootless，无 sudo）。

**注①**：gateway 不发布端口，验它连 vLLM → `sudo docker exec deer-flow-gateway python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8099/v1/models',timeout=5).status)"` 回 `200`。

**日志/进容器**：`sudo docker logs -f <容器名>`、`sudo docker exec -it <容器名> sh`；rootless 的（`ce-postgres`/`ce-milvus`/`langfuse-langfuse-web-1`）去 sudo。

**daemon**：`sudo docker`=系统 daemon（GPU/app/监控）；`docker`（无 sudo）=rootless（仅 deps 数据层 + Langfuse）。

---

## 2. 启动（按依赖顺序）

首次先建各 `.env`（从 `.env.example` 复制）：`docker/ce-services/.env`、`docker/ce-rerank/.env`、`docker/ce-monitoring/.env`。

```
# ① 数据层（rootless，无 sudo）
docker compose -f docker/ce-code/docker-compose.deps.yaml up -d
# ② GPU 模型 embed:8097 / vLLM:8099 —— 外部，须先在跑（curl localhost:8097/v1/models 确认）
# ③ 精排（sudo，GPU）
sudo docker compose -f docker/ce-rerank/docker-compose.yaml --env-file docker/ce-rerank/.env up -d
# ④ 知识+任务（sudo，host-net；include 一并起 ce-code）
sudo docker compose -f docker/ce-services/docker-compose.yaml --env-file docker/ce-services/.env up -d
# ⑤ harness（sudo）；首次先改 config.yaml 的 base_url 为 host.docker.internal
sed -i 's#http://localhost:8099/v1#http://host.docker.internal:8099/v1#' config.yaml
sudo ./scripts/deploy.sh
# ⑥ 监控（sudo）
sudo docker compose -f docker/ce-monitoring/docker-compose.yaml --env-file docker/ce-monitoring/.env up -d
```

---

## 3. 登录 / 账号密码，应用网页（nginx :2026）——给造价用户用
- 访问：`http://172.19.3.136:2026`（本机可 `localhost:2026`）
- **无默认密码**。首次走 `/setup` 自建管理员；已建则用你设的。
- **忘密码重置**（harness 在 sudo daemon，命令**加 sudo**）：
  ```
  sudo docker compose -p deer-flow -f docker/docker-compose.yaml exec gateway sh -c "cd backend && PYTHONPATH=. uv run python -m app.gateway.auth.reset_admin"
  sudo docker compose -p deer-flow -f docker/docker-compose.yaml exec gateway cat /app/backend/.deer-flow/admin_initial_credentials.txt
  ```
  凭据文件里是新邮箱+密码，登录后按引导设成自己的。多管理员加 `--email you@example.com`。
---

## 4. 健康检查

**一键自检（应用 / 数据 / gateway，宿主机执行）：**
```
for p in 8100 8101 8095 8097 8099; do printf "$p -> "; curl -s -o /dev/null -w "%{http_code}\n" localhost:$p/health 2>/dev/null || echo down; done
curl -s localhost:9091/healthz && echo " milvus-ok"                        # Milvus
curl -s -o /dev/null -w "nginx:2026 -> %{http_code}\n" localhost:2026      # 307=活
sudo docker exec deer-flow-gateway python -c "import urllib.request;print('gateway->vLLM',urllib.request.urlopen('http://host.docker.internal:8099/v1/models',timeout=5).status)"
```
> 单服务的验证 / 日志 / 容器名见 §1 拓扑表。**监控健康 + Prometheus 无数据 / Grafana 空看板排障 → `docker/ce-monitoring/README.md`。**

---

## 5. 停止 / 更新

```
# 停（各自 compose down；deps 谨慎、别 -v）
sudo ./scripts/deploy.sh down                                               # harness（sudo）
sudo docker compose -f docker/ce-services/docker-compose.yaml down          # 知识+任务
sudo docker compose -f docker/ce-rerank/docker-compose.yaml down            # 精排
sudo docker compose -f docker/ce-monitoring/docker-compose.yaml down        # 监控
docker compose -f docker/ce-code/docker-compose.deps.yaml down              # 数据（有状态，慎）
```
**代码更新**：`git pull` → 涉及哪层就 `[sudo] docker compose -f <该层> up -d --build`。

---
