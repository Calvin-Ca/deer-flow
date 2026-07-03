# CE 生产部署总览（服务器 172.19.3.136）

深圳房建组价 Agent 全栈的 Docker 部署、启动顺序、账号密码。**全 Docker**（2026-07-03 定）。

> 拓扑决策与 rootless 三个坑详见记忆 `project_ce_deploy_topology`；本文是操作手册。

---

## 1. 拓扑一览

| 服务 | 端口 | daemon | compose / 起法 |
|---|---|---|---|
| nginx（入口）| **2026** | rootless | `make up` |
| gateway（API+agent）| 8001 | rootless | `make up` |
| 前端 | (内部3000) | rootless | `make up` |
| 知识 knowledge | **8100** | **sudo** host-net | `docker/ce-services/docker-compose.yaml`（include ce-code）|
| 任务 tasks | **8101** | **sudo** host-net | 同上 |
| 精排 rerank | 8095 | **sudo** GPU | `docker/ce-rerank/docker-compose.yaml` |
| embed | 8097 | sudo GPU | 外部（非本项目 compose，须先起）|
| vLLM Qwen3-8B | 8099 | sudo GPU | 外部（须先起）|
| PostgreSQL `ce_cost` | 5433 | rootless | `docker/ce-code/docker-compose.deps.yaml` |
| Milvus | 19530 (+9091) | rootless | 同 deps |
| Prometheus | 19090 | sudo host-net | `docker/ce-monitoring/docker-compose.yaml` |
| Grafana | 3001 | sudo | 同 monitoring |
| Langfuse（可选，对话观测）| 3030 | sudo | `docker/ce-langfuse/docker-compose.yaml` |

**daemon 约定**：`sudo docker` = 系统 daemon（app/rerank/models/监控在这，GPU 一律 sudo）；不带 sudo = 用户 rootless daemon（deps + harness 在这）。GPU 躲不开 sudo（rootless GPU 撞 cgroup 权限）。

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
# ⑤ harness gateway+前端+nginx（rootless）
make up
# ⑥ 监控（sudo）
sudo docker compose -f docker/ce-monitoring/docker-compose.yaml --env-file docker/ce-monitoring/.env up -d
```

**开机自启**：sudo 容器 + harness 容器均带 `restart: unless-stopped`（随各自 docker daemon 自起）；rootless daemon 开机常驻需 `sudo loginctl enable-linger caic`。**不用 systemd**（`scripts/systemd/` 是废弃的裸机备选，勿装——会和容器抢端口）。

---

## 3. 登录 / 账号密码

### 3.1 应用网页（nginx :2026）——给造价用户用
- 访问：`http://172.19.3.136:2026`（本机可 `localhost:2026`）
- **无默认密码**。首次走 `/setup` 自建管理员；已建则用你设的。
- **忘密码重置**（harness 在 rootless，命令**不加 sudo**）：
  ```
  docker compose -p deer-flow -f docker/docker-compose.yaml exec gateway sh -c "cd backend && PYTHONPATH=. uv run python -m app.gateway.auth.reset_admin"
  docker compose -p deer-flow -f docker/docker-compose.yaml exec gateway cat /app/backend/.deer-flow/admin_initial_credentials.txt
  ```
  凭据文件里是新邮箱+密码，登录后按引导设成自己的。多管理员加 `--email you@example.com`。

### 3.2 Grafana（:3001）——监控面板
- 用户 `admin` / 密码 = `docker/ce-monitoring/.env` 的 `GRAFANA_PASSWORD`（未设则默认 `admin`）。
- 只在首次启动（数据卷为空）用该 env 初始化；改密：`sudo docker exec -it ce-grafana grafana cli admin reset-admin-password 新密码`。

### 3.3 数据层（内网、非公网）
- **PostgreSQL** `localhost:5433`：库 `ce_cost` / 用户 `cost` / 密码 `caic`（`POSTGRES_PASSWORD` 可覆盖）。
- **Milvus** `localhost:19530`（无鉴权）；其 minio 后端 `minioadmin/minioadmin`。
- 这些是内网默认值；如需改，设对应 env 后重建 deps（注意数据卷）。

### 3.4 Prometheus（:19090）/ Langfuse（:3030）
- Prometheus 无鉴权（内网）。
- Langfuse 自托管首次在 UI 建账号（`LANGFUSE_INIT_USER_EMAIL`，见 `benchmark/LANGFUSE.md`）。

---

## 4. 健康检查

```
# 应用/数据/GPU 服务
for p in 8100 8101 8095 8001 8097 8099; do printf "$p -> "; curl -s -o /dev/null -w "%{http_code}\n" localhost:$p/health 2>/dev/null || echo down; done
curl -s localhost:9091/healthz && echo " milvus-ok"        # Milvus
# 监控
curl -s localhost:19090/-/healthy; echo                     # Prometheus
for p in 9400 9100 9115; do printf "$p "; curl -s -o /dev/null -w "%{http_code}\n" localhost:$p/metrics; done  # dcgm/node/blackbox
```
Grafana 看板（Import ID）：**1860** 主机 / **12239** GPU / **7587** 端点存活。Prometheus `:19090` → Status→Targets 看各 job UP。

---

## 5. 停止 / 更新

```
# 停（各自 compose down；deps 谨慎、别 -v）
make down                                                                   # harness
sudo docker compose -f docker/ce-services/docker-compose.yaml down          # 知识+任务
sudo docker compose -f docker/ce-rerank/docker-compose.yaml down            # 精排
sudo docker compose -f docker/ce-monitoring/docker-compose.yaml down        # 监控
docker compose -f docker/ce-code/docker-compose.deps.yaml down              # 数据（有状态，慎）
```
**代码更新**：`git pull` → 涉及哪层就 `[sudo] docker compose -f <该层> up -d --build`。

---

## 6. 常见坑（都踩过）
1. **GPU 容器 rootless 起不来**（`devices.allow: permission denied`）→ 跑 sudo daemon。
2. **rootless host-net ≠ 真宿主**：跨 daemon 用 LAN IP 或改 sudo host-net（app 已全 sudo，故 localhost 互通）。
3. **裸机进程影子化容器**：旧 `uv run` 的 :8100/:8101 若还活着会占真宿主端口 → `sudo ss -ltnp | grep :810x` 查绑定进程。
4. **镜像拉取**：ce 三镜像 pip 已配清华源；gcr.io 镜像（cadvisor）国内墙 → 默认关闭（`--profile cadvisor` 开）。
5. **端口占用**：起前 `sudo ss -ltnp | grep :<port>`（Prometheus 因 :9090 被占已挪 :19090）。
6. **前端→ce-services / gateway→ce-MCP**：容器化前端/网关经 `host.docker.internal` 或 LAN IP 触达 ce（localhost 在桥接容器里指自身）。

镜像加速 pip 源：ce 三 Dockerfile 带 `ARG PIP_INDEX_URL=清华源`。
