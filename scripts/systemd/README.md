# CE 裸机服务开机自启（systemd）

CE 生产拓扑（见记忆 `project_ce_deploy_topology`）里 **知识 ce-rag :8100 是裸机 `uv run`**，
setsid 起的进程不扛重启。用 `ce-knowledge` systemd 单元让它开机自启 + 崩溃自拉起。
（原 **任务层 :8101 已整体退役**，`ce-tasks.service` 已删。**ce-db :8102** 本仓暂无 systemd 单元——
如需 docker 部署见 `docker/ce-code/docker-compose.yaml`（含 ce-rag + ce-db）；要裸机自启可照
`ce-knowledge.service` 仿写一个 `ExecStart=... -m service.db_api` 的单元。）

## 安装（服务器，sudo）
```
sudo cp scripts/systemd/ce-knowledge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ce-knowledge
```
查看 / 日志：`systemctl status ce-knowledge` · `journalctl -u ce-knowledge -f`

⚠️ **装前先停掉手动起的裸机进程**（避免端口占用）：`lsof -ti:8100 | xargs -r kill`

## 依赖服务的开机自启（Docker 侧，不用 systemd）
- **rerank :8095 / embed / vLLM**（sudo 系统 daemon）：容器带 `restart: unless-stopped`，随系统 docker 开机自起。
- **PG :5433 / Milvus :19530**（rootless daemon）：容器带 restart 策略，但 rootless daemon 默认随用户登录才起。
  开机自起需 `sudo loginctl enable-linger caic`（让 caic 的 user manager 开机常驻）。

## 启动顺序说明
裸机服务 `After=docker.service`，但 rootless deps 与裸机服务可能有竞态（deps 没起时知识层连 Milvus/PG 报错）。
两单元均 `Restart=on-failure`：deps 就绪后会自动重试拉起，最终收敛。精排 :8095 未就绪时知识层降级 RRF（不报错）。

## uv 路径
ExecStart 用 `bash -lc 'exec uv run ...'` 取登录 PATH 里的 uv。若启动报 `uv: command not found`，
`which uv` 看绝对路径后替换 ExecStart（如 `/home/caic/.local/bin/uv run ...`）。
