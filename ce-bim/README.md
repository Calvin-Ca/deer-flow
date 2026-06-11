# ce-bim（BIM 底座层）

项目 BIM 模型的**单一 owner + BIM 原语服务**。本文件只**涉及**：目录结构、原语契约、起服务。

> - 需求/设计（为什么独立成层、GlobalId 连接键、消费方矩阵、端点规格）见 `PRD.md`
> - 依赖服务与环境（IfcOpenShell/MinIO/端口/前端 viewer 选型）见 `DEV.md`
> - 进度与分阶段见 `TODO.md`
> - 项目级共享上下文（设备分工、git 约定、服务器环境）见仓库根 `CLAUDE.md`
> - **消费方**：算量计价 `../ce-cost/`（第一个）、审图轨（规划）等，均是本服务的纯 HTTP 客户端

⚠️ **本层为 greenfield**：以下目录/命令为 Phase 1 目标骨架，进度见 `TODO.md`。

---

## 在拓扑中的位置

```
ce-code 知识服务 :8100   (规范/造价知识：检索原语，retrieval 唯一 owner)
ce-bim  BIM 底座 :8102   (项目 BIM 模型：BIM 原语，IfcOpenShell + IFC 原件唯一 owner)  ◀ 本层
        ▲ HTTP /model /elements /quantity /spatial（按 GlobalId）
        │
消费方（纯 HTTP 客户端，各自叠加业务逻辑）：
  ce-cost     算量计价   (取量 → 扣减/清单/组价)        ← 第一个消费方
  审图轨       规范合规   (取构件属性 → 与 ce-code 强条谓词比对)   ← 规划
  FM / 4D     运维/施工  (取空间结构 + 属性)             ← 未来
```

> 与 `ce-code` 的分工：`ce-code` 管**通用规范/造价知识**（跨项目标准），`ce-bim` 管**项目 BIM 模型实例**（某项目几何/构件，按 GlobalId 寻址）。两者正交。

---

## 目录结构（Phase 1 目标）

```
ce-bim/
├── README.md                  # 本文件（操作手册）
├── PRD.md / DEV.md / TODO.md   # 需求设计 / 开发环境 / 进度
├── pyproject.toml             # 独立 uv 项目（fastapi/uvicorn/ifcopenshell/minio/pydantic）
├── store/                     # IFC 原件存 MinIO + 解析产物落盘
├── parse/                     # IfcOpenShell：构件 + 基础几何量 + 属性 + 空间结构（带 GlobalId）
├── api/                       # BIM 原语 HTTP（:8102）
└── main.py                    # 统一入口 :8102
```

> 前端 viewer 是**独立的共享组件包 `ce-bim-viewer`**（web-ifc + Three.js + Vue3），不在本后端目录内；各产品复核台 `import` 它。见 `PRD.md §3`。

---

## BIM 原语（按 GlobalId 寻址）

```
POST /model/ingest                上传/登记 IFC → 存 MinIO + 解析建索引 → 返回 model_id
GET  /model/{id}                  取 IFC 原件（前端 web-ifc 渲染拉取）
GET  /model/{id}/elements         列构件（按 type/storey 过滤），每项带 GlobalId
GET  /model/{id}/element/{guid}   单构件：属性 + 基础几何量 + 空间归属
POST /model/{id}/quantity         按 GlobalId 批量取基础几何量（喂 ce-cost 算量引擎）
GET  /model/{id}/spatial          空间结构树（项目→场地→楼栋→层→构件）
GET  /health                      含 store / parser 依赖地址
```

**边界**：不做扣减/清单/组价（→ `ce-cost`）、不做规范校验/碰撞/4D（→ 消费方）、不做几何编辑（只读取数 + 存储）。

---

## 起服务（Phase 1 后）

BIM 底座**自身不依赖** :8100 / :8101，可独立起；消费方依赖它，需先起底座。

```bash
cd ce-bim && uv sync && uv run python main.py   # :8102 BIM 底座
curl http://localhost:8102/health
```

> 后台常驻**勿用 nohup**（服务器 Exit 125 静默失败），用 `setsid` 或 tmux。
