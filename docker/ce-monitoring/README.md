# CE 监控栈（Prometheus + Grafana）

系统/GPU/容器/端点存活监控。跑 **sudo 系统 daemon**（dcgm 需 GPU）、host 网络。
LLM/agent 层的观测（trace/token/选码/反馈）走 **Langfuse**（`docker/ce-langfuse`），两者互补：
这套盯"机器/服务健康"，Langfuse 盯"对话质量"。

## 起
```
cp docker/ce-monitoring/.env.example docker/ce-monitoring/.env    # 改 GRAFANA_PASSWORD
sudo docker compose -f docker/ce-monitoring/docker-compose.yaml --env-file docker/ce-monitoring/.env up -d
```
- **Grafana**：http://172.19.3.136:3001 （admin / 你设的密码）
- **Prometheus**：http://172.19.3.136:19090 （Status→Targets 看各 exporter 是否 UP）

## 采集内容 / 端口
| exporter | 端口 | 采集 |
|---|---|---|
| dcgm-exporter | 9400 | 4x RTX 4090 利用率/显存/温度/功耗（**最该盯**：3 个 GPU 服务抢卡防 OOM）|
| node-exporter | 9100 | 主机 CPU/内存/磁盘/网络 |
| cadvisor | 8081 | 每容器 CPU/内存/网络（rootless + sudo 两 daemon 全见，读 cgroup）|
| blackbox-exporter | 9115 | 各服务 `/health` 存活 + 时延（:8100/8101/8095/8001/8097/8099）|
| Milvus 自带 | 9091 | Milvus 指标 |
| Prometheus/Grafana | 9090/3001 | 自身 |

> 起前确认这些端口未被占：`sudo ss -ltnp | grep -E ':19090|:3001|:9100|:8081|:9400|:9115'`。

## Grafana 看板（起栈后在 UI 导入）
Prometheus 数据源已自动配好。看板按 ID 导入（Grafana → Dashboards → Import → 输入 ID → 选 Prometheus 源）：
- **1860** Node Exporter Full（主机）
- **12239** NVIDIA DCGM Exporter（GPU）
- **14282** Cadvisor（容器）
- **7587** Blackbox Exporter（端点存活）
> 导入需服务器能联网拉 grafana.com；不通则从有网机器导出 JSON 丢进 grafana 数据卷 `/var/lib/grafana/dashboards`（provider 已配自动加载）。

## 备注
- **dcgm 镜像 tag**：`3.3.5-3.4.0-ubuntu22.04` 配驱动 535.x；若拉取/启动失败，换 dcgm-exporter 兼容你驱动的 tag。
- 全 host 网络 + sudo daemon：Prometheus `localhost:<port>` 直达各 exporter；cadvisor 读 cgroup 故两个 docker daemon 的容器都统计到。
- 停：`sudo docker compose -f docker/ce-monitoring/docker-compose.yaml down`（数据在 named volume，删卷才丢历史）。
