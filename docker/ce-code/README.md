# CE 数据层（deps：PostgreSQL + Milvus）

`docker/ce-code/docker-compose.deps.yaml` 起知识层依赖的**有状态数据服务**，跑 **rootless docker（caic，无 sudo）**，端口发布到真宿主，供 sudo host-net 的知识/任务容器用 `localhost` 命中。

## 服务与端口

| 服务 | 端口 | 容器 |
|---|---|---|
| PostgreSQL `ce_cost` | 5433 | `ce-postgres` |
| Milvus | 19530（+9091 metrics）| `ce-milvus` |
| Milvus etcd | 仅容器内 | `ce-milvus-etcd` |
| Milvus minio | 仅容器内 | `ce-milvus-minio` |

## 账号 / 密码（内网、非公网默认值）

- **PostgreSQL** `localhost:5433`：库 `ce_cost` / 用户 `cost` / 密码 `caic`（`POSTGRES_PASSWORD` 可覆盖）。
- **Milvus** `localhost:19530`（无鉴权）；其 minio 后端 `minioadmin / minioadmin`。
- 改默认值：设对应 env 后重建 deps（**注意数据卷**，别 `-v`）。

## 起停（rootless，无 sudo）

```
docker compose -f docker/ce-code/docker-compose.deps.yaml up -d      # 起
docker compose -f docker/ce-code/docker-compose.deps.yaml down       # 停（保留数据卷）
```
- `down` **别加 `-v`**——有状态、删卷丢库。
- 日志：`docker logs -f ce-postgres` / `docker logs -f ce-milvus`（**rootless，无 sudo**）。
- **reboot 存活**：rootless 需 `sudo loginctl enable-linger caic` + `systemctl --user enable docker`。

> 拓扑全景 / 启动顺序 / 其余服务账号见 `docker/README.md`。
