# ce-bim（BIM 底座层）· 开发文档

> BIM 底座层开发的**依赖服务与环境**。需求/设计见 `PRD.md`，进度见 `TODO.md`，操作命令（起服务）见 `README.md`，项目级共享约定（git/设备分工/服务器）见根 `CLAUDE.md`。

---

## 依赖服务

BIM 底座 = 模型存储 + 解析 + 原语，用到以下服务（多为待部署）：

| 角色 | 选型 | 地址 | 用途 | 备注 |
|---|---|---|---|---|
| IFC 解析/几何/取量 | IfcOpenShell | 进程内库（CPU） | 按 GlobalId 提取构件/基础几何量/属性/空间结构 | **无需 GPU**；底座是轻服务 |
| 对象存储 | MinIO | 待部署 | 存 IFC 原件 + 解析产物（前端渲染 + 后端取量同一份） | 与 `ce-cost` 复用同一实例 |
| 解析索引（可选） | PostgreSQL | 待部署 | 缓存构件索引供 `/elements` 过滤查询 | **P0 可先落 JSON/对象存储，P1 再入 PG** |
| BIM 原语服务 | FastAPI + uvicorn | `http://localhost:8102` | 对外暴露 `/model` `/elements` `/quantity` `/spatial` | 独立 uv 项目 |

> 关系库/向量库/Embedding/VLM 是 `ce-code` 知识层资产，BIM 底座**一概不碰**；造价扣减/清单/组价在 `ce-cost`，规范校验在审图轨，均不在本层依赖范围。

**依赖健康自检（命令单行）：**

- MinIO：`curl -s http://localhost:9000/minio/health/live`
- BIM 底座自身：`curl -s http://localhost:8102/health`（含 store / parser 依赖地址）

---

## 前端 viewer 包（`ce-bim-viewer`）依赖

> "查看/操作"做成共享前端组件包，与后端底座分离（见 `PRD.md §3`）。

| 角色 | 选型 | 备注 |
|---|---|---|
| IFC 渲染/解析 | web-ifc / `@thatopen/components`（IFC.js 系） | WASM 浏览器端解析 IFC，GlobalId 原生可取 |
| 3D 渲染 | Three.js | 由 web-ifc 驱动 |
| 框架 | Vue 3 | 与各产品复核台前端栈一致 |
| 大模型升级（P1） | xeokit + XKT（`xeokit-convert` / `ifc2gltf`） | >100MB 模型性能瓶颈时切换；转换保 GlobalId |

> ❌ Autodesk APS/Forge（云端 SaaS）违反内网私有化 + 数据合规，不用（见 `PRD.md §3`）。

---

## 开发环境要点（BIM 底座专属）

- **独立 uv 项目**：核心依赖 `fastapi`/`uvicorn`/`ifcopenshell`/`minio`/`pydantic`，首次 `cd ce-bim && uv sync`
- **无需 GPU / torch / Milvus**：IfcOpenShell 走 CPU，底座可极轻量部署
- **包管理**：`uv add` 管理依赖，**严禁 `uv pip install`** 绕过 `pyproject.toml`
- **端口约定**：底座占 `:8102`（:8100 知识 / :8101 任务 / **:8102 BIM**）

> 共享环境基础（服务器路径、Python 版本、uv 版本、GPU 硬件）见根 `CLAUDE.md §2.3`。

---

## 起服务

BIM 底座服务（:8102）启动命令见 `README.md`。底座**自身不依赖** :8100 / :8101，可独立起；消费方（`ce-cost` 等）依赖它，需先起 BIM 底座。
