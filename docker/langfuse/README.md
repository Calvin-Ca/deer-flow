# Langfuse 自托管（deer-flow 追踪后端）

LLM 可观测性：看每次模型调用的真实 system prompt、消息历史、工具调用（`read_file`/`bash`/`task` 等）、token、延迟、报错。开源自托管，**免费、数据不出内网**。

## 启动（在跑 deer-flow 的服务器上）

```bash
cd docker/langfuse
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
