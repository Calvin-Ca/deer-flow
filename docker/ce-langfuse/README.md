# Langfuse 自托管（deer-flow 追踪后端）

LLM 可观测性：看每次模型调用的真实 system prompt、消息历史、工具调用（`read_file`/`bash`/`task` 等）、token、延迟、报错。开源自托管，**免费、数据不出内网**。

## 启动（在跑 deer-flow 的服务器上）

```bash
cd docker/ce-langfuse
docker compose up -d          # 首次拉镜像 + 初始化，约 1-2 分钟
docker compose ps             # 等 langfuse-web 变 healthy
```

- Web UI：`http://<服务器IP>:3030`（3000 留给 deer-flow 前端）
- 登录：`admin@deerflow.local` / 密码见 `.env.example` 里的 `LANGFUSE_INIT_USER_PASSWORD`
- 远程访问：把 `NEXTAUTH_URL` 改成 `http://<服务器IP>:3030`（在 `.env` 里覆盖），否则登录回调失败

> compose 内置了全部默认密钥，**不放 `.env` 也能直接跑通**。要改密钥/端口/账号，`cp .env.example .env` 后编辑（`.env` 已被 git 忽略）。

## 让 deer-flow 上报到这里

在 deer-flow 跑的机器的根 `.env` 里取消注释这四行（值已与本栈初始化默认值对齐）：

```bash
LANGFUSE_TRACING=true
LANGFUSE_PUBLIC_KEY=pk-lf-f8233b5e-1a4b-4561-ad52-40ed09152898
LANGFUSE_SECRET_KEY=sk-lf-9a357125-10bc-42da-a4a0-f520026bd5b7
LANGFUSE_BASE_URL=http://localhost:3030
```

然后**重启 Gateway**（env 仅启动时读）：`make gateway` 或 `make dev`。发一条消息后，Langfuse UI 的 Traces 里就会出现这次运行的完整链路。

## 停止 / 清空

```bash
docker compose down            # 停服务，保留数据
docker compose down -v         # 连数据卷一起删（trace 全清）
```

## 端口与冲突

只有 `3030`（Web UI）对外暴露。Postgres / ClickHouse / Redis / MinIO 全部仅容器内网可见，**不占用宿主端口**，与 vLLM(8097-8099)/Milvus(19530)/RAG(8100/8101) 互不冲突。

## 账号 / key / rootless 运维（生产 = docker gateway，2026-07-03 定案）

> 以下为**全 Docker 生产**的权威做法，取代上文 bare-metal 的 `make gateway` / `localhost:3030` 说法。

- **Langfuse 跑 rootless daemon**（`docker compose -p langfuse ... up -d`，**无 sudo**）；数据是 named volume，`down -v` 会清空。**reboot 存活**：`sudo loginctl enable-linger caic` + `systemctl --user enable docker`（不加 sudo）。
- **建账号/项目**：首次 UI `http://172.19.3.136:3030` → `/setup` 建账号 → 建项目 → **Create API keys**（secret **只显示一次**，当场存好）。
- **key 填 gateway `.env`**：`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 用刚建的**一对**，**必须和当前项目里某对有效 key 完全一致**（不一致 → gateway 上报 401，trace 进不去）。
- **gateway 是 bridge 容器**：`LANGFUSE_BASE_URL=http://host.docker.internal:3030`（**不能用 `localhost`**，容器里指自身）。
- **改完必须 recreate gateway**（`.env` 是创建容器时注入的环境变量，`docker restart` **不重读**）：`sudo ./scripts/deploy.sh start` 或 `sudo docker compose -p deer-flow -f docker/docker-compose.yaml up -d --force-recreate gateway`。核对生效：`sudo docker exec deer-flow-gateway env | grep -i langfuse`。
- 日志：`docker logs -f langfuse-langfuse-web-1`（**rootless，无 sudo**）。
