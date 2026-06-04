# ce-services（任务层）· 进度 TODO

> 任务层（生成 + 编排）的执行进度与重构历程。需求/设计见同目录 `PRD.md`；知识层进度见 `../ce-code/TODO.md`。

---

## 阶段 1：code-qa skill 封装（✅ 已完成）

- [x] 结构化输出 + 强制引用（`generation.py`；Qwen3-8B vLLM；强条/推荐性显式区分）
- [x] 封装为 `skills/public/code-qa/`（SKILL.md + qa.py；端到端验证通过）

## 阶段 2：项目级合规审查（✅ 已完成）

- [x] `params.py`：Qwen3 从自由文本提取结构化建筑参数（类型/高度/面积/用途/建筑类别）
- [x] `queries.py`：规则驱动按合规维度展开 8-16 个检索查询（高层/地下/特殊用途条件触发）
- [x] `orchestration.py`：端到端编排——并行检索 → 按维度串行去重判定 → 反思校验
- [x] 封装为 `skills/public/compliance-check/`（SKILL.md + check.py；端到端验证通过）
- [x] 端到端验证：32m 二类高层住宅 → **85 条强条 / 15 维度**，反思校验正确捕获遗漏维度
- [x] deer-flow sub-agent 集成：`compliance-checker` 注册为 config.yaml 自定义 agent；skill 含多轮对话编排
- [x] deer-flow 模型切换为本地 Qwen3-8B（全链路无需 OpenAI API）

---

## 重构历程（行为保持，不改 schema、不重建索引）

### skill HTTP 服务化（✅）

原 skill 在沙箱里 `cd /mnt/nvme/...` + 动态加载 POC 脚本，因 deer-flow 沙箱只挂载 `skills/`（POC venv/脚本/数据均不可见）而崩溃 → 改为常驻 HTTP 服务 + skill 侧退化为**纯标准库 urllib HTTP 客户端**（沙箱内零依赖）。

### v3：任务层迁出知识层（✅ 2026-06-03）

把生成/编排从 ce-code 彻底拆出，落地"一个知识服务，N 个任务服务"拓扑。

- [x] 新建顶层 `ce-services/`（独立 uv 项目，仅 fastapi/uvicorn/requests/pydantic）
- [x] `common/`：`config.py` + `knowledge_client.py`（打 :8100 `/search`）
- [x] `qa/generation.py`、`compliance/{orchestration,params,queries}.py` 从 ce-code 平移
- [x] 关键改动：`orchestration._get_retrieve_fn` 从进程内 `retrieval.engine.search` 改为 `knowledge_client.search`（HTTP）
- [x] **服务器验证**：`uv sync` 成功；`/health` service 字段正确；`07_eval.py` 召回率不变；六端点 + 两 skill 客户端回归通过

> 此轮拆分后任务层是 qa（:8102）+ compliance（:8101）两个独立进程。

### Docker（✅ 2026-06-04）

- [x] `docker/ce-services/`：任务服务镜像（python:3.12-slim，~200MB）+ 全栈 compose（`include` ce-code + `depends_on: service_healthy`）
- [x] `network_mode: host` 直连宿主机 Milvus/vLLM

### 任务层合并为单进程单端口（✅ 2026-06-04）

qa（:8102）+ compliance（:8101）两进程合并为单一进程，共用 :8101。

- [x] 新增 `qa/router.py` + `compliance/router.py`（从各自 server.py 提取 APIRouter）
- [x] 新增 `main.py`（统一入口，`include_router` 两个 router，监听 :8101）
- [x] `server.py` 退化为独立测试用薄包装（import router）
- [x] Docker：两个 service → 单个 `tasks` service，CMD 改 `main:app`
- [x] skill `code-qa/qa.py` 默认地址 8102 → 8101
- [x] `curl localhost:8101/health` 返回 `service:"tasks", routes:["/qa","/compliance"]`
- [x] `curl -X POST :8101/qa` 和 `/compliance` 全绿（/qa 实测 43 条全强条 + 结构化回答）
- [ ] 两 skill 客户端回归通过（最后一项待跑）

**部署变化累积**：起 1 进程 → 2（Phase A）→ 3（v3 拆分）→ **2（本次合并：知识 :8100 + 任务 :8101）**。

---

## 阶段 3：设计辅助（⬜ 待办）

**目标**：实时给出参数约束（如"此建筑高度下最大防火分区面积"）。
- [ ] 在 sandbox 中执行规范计算公式（疏散宽度、防火间距等；依赖知识层 Phase B 的 `formulas` 字段）
- [ ] 与设计工具（CAD/BIM）潜在集成
