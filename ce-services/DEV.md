# ce-services（任务层）· 开发文档

> 任务层开发的**依赖服务与环境**。需求/设计见 `PRD.md`，进度见 `TODO.md`，操作命令（起服务）见 `README.md`，项目级共享约定（git/设备分工）见根 `CLAUDE.md`。

---

## 依赖服务

任务层 = 生成 + 编排，是知识服务的纯 HTTP 客户端，只用到：

| 角色 | 模型 / 服务 | 地址 | 任务层用途 | 备注 |
|---|---|---|---|---|
| 文本生成 / 推理 | Qwen3-8B | `http://localhost:8099`，model_id `qwen3-8b` | 问答生成、合规判定、反思校验、参数提取 | `/think` 启用 thinking、`/no_think` 禁用；JSON 输出建议 `/no_think` |
| 检索（内部依赖） | 知识服务 | `http://localhost:8100` | 打 `/search` 拿裸条款 | 由 `common/knowledge_client.py` 封装；必须先起 |

> 任务层**不直连** Embedding / Milvus / VLM —— 那些是知识层（`../ce-code/`）的资产，任务层一概不碰。

---

## 配置（env 覆盖，见 `common/config.py`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BCRAG_KNOWLEDGE_URL` | `http://localhost:8100` | 知识服务地址 |
| `BCRAG_LLM_URL` | `http://localhost:8099` | Qwen3-8B vLLM 地址 |
| `BCRAG_LLM_MODEL_ID` | `qwen3-8b` | LLM model_id |
| `CE_HITL_CHECKPOINT_DB` | `ce-services/.hitl_checkpoints.db` | HITL 图 SqliteSaver 持久化文件（gitignore，可重生成）|
| `CE_HITL_CONFIDENCE_TAU` | `0.75` | 编码闸置信阈值 τ：≥τ 且无多候选才自动过（保守起步偏高）|

---

## 开发环境要点（任务层专属）

- **独立 uv 项目**：依赖较轻（`fastapi`/`uvicorn`/`requests`/`pydantic` + HITL 图的 `langgraph`/`langgraph-checkpoint-sqlite`），首次 `cd ce-services && uv sync`
- **不依赖 GPU / torch / Milvus 客户端**：镜像可极轻量（Docker tasks 镜像 ~200MB）
- **包管理**：`uv add` 管理依赖，**严禁 `uv pip install`** 绕过 `pyproject.toml`

> 共享环境基础（服务器路径、Python 版本、uv 版本）见根 `CLAUDE.md` §2.3。

---

## 起服务

任务服务（:8101，`/qa` + `/compliance` 共进程）启动命令（含 Docker 全栈）见 `README.md`。**前置：知识服务 :8100 必须先起。**

---

## HITL 可中断组价图（langgraph，2026-06-28）

> 设计见 `HITL_DESIGN.md`。已落地 §9 路径**步1（provenance 信封）+ 步3（图骨架）+ 步5（后段全节点）**：
> 全 13 步链路 `setup → 编码 → 定额 → 信息价 → 费率 → 项目参数 → 总造价(末尾 review)` + interrupt/resume +
> checkpointer，curl 驱动无头闭环；前端后续接。后段三节点（§8 费率 / §10⑪§12 参数 / §13 rollup）均**确定性算钱、
> 无 LLM**（`compute_unit_price` / `rollup_cost`），故 interrupt 与计算同节点、resume 重跑无漂移（不必双拆）。

**为什么独立成图**：13 步组价的两类刚需（展示决策依据 / 中途等输入再继续）黑盒 `cost.py` 都做不到——
编排上提成**可中断 langgraph 状态机**，每数字带结构化来源（provenance 信封），每介入点是可暂停可恢复的闸门。
与 `/cost/compose` 端到端旧路**并存**（判据=是否需 HITL/可审计）。

**模块**（均在 `cost/`）：
| 文件 | 职责 |
|---|---|
| `provenance.py` | §5.1 信封 + 原语适配器（原地包现有 `bill_match`/`select_code`/`price_compose`，不重写）|
| `state.py` | §5.4 任务状态 schema（`CostTaskState` TypedDict）+ 纯函数 helper（lock_value/audit/override）|
| `gates.py` | §6 门控（是否跳闸，全在代码、不交弱模型）+ §5.2/5.3 interrupt payload + 决策落值 |
| `graph.py` | langgraph StateGraph：上游 compute/gate **双拆**（LLM 调用放 compute 节点避免 resume 漂移）；后段费率/参数/汇总确定性算钱、同节点 interrupt |
| `pricing.py` | 确定性算钱原语：`compute_unit_price`（§8 综合单价）+ `rollup_cost`（§13 总造价汇总），各带 pydantic 闸门、不杜撰费率/税率 |
| `session.py` | 图 + SqliteSaver 单例 + start/resume/get_state（透出 rates/params/rollup）|

**新增依赖**（服务器侧落锁，本地不提交 uv.lock）：
`cd ce-services && uv add langgraph langgraph-checkpoint-sqlite`

**端点 / 跑法**（:8101）：
- `POST /cost/session/start`（body `{feature, spec, region, period?, price_source?, rates?}`）→ 跑到首个闸或 done。
- `POST /cost/session/{task_id}/resume`（body `{decision}`）→ 续到下个闸或 done。
- `GET  /cost/session/{task_id}/state` → 读持久化状态（已钉编码/override/audit_log；重启进程仍在）。

**本地验证边界**：langgraph/服务/LLM 本地不全 → 本地只 `python3 -m py_compile cost/*.py` + `gates`/`state` 纯函数单测；
图 + 取数真跑在服务器。

---

## 组价能力对外暴露：skill / tool / MCP 分层方案（决策，2026-06-27）

> 背景：把"智能组价"拆成 7 步后，逐步定位到 skill / tool / MCP 三种暴露形态。三者对模型的区别要先钉死——
> **tool 与 MCP 对模型是同一个东西**（都是 function-calling 表面，带 `args_schema` 那道 pydantic 校验闸门），
> 区别在**实现拓扑**：tool 进程内、强校验、私有、与 agent 同生命周期；MCP 独立服务、可跨消费方复用、独立版本化/运维。
> **skill** 是另一个维度——方法论 playbook + bash 脚本，承载流程知识（红线/呈现/HITL），渐进披露，且在弱
> function-calling 模型上把"一次复杂嵌套 args 的调用"降为"一次 bash 字符串调用"（Qwen3-8B 生成 shell 串比生成合
> schema 的嵌套 JSON 稳得多）。skill 最终仍骑在 bash 这个 tool 上。

### 7 步 → 形态映射

| 步骤 | 性质 | 形态 | 落点 / 理由 |
|---|---|---|---|
| 1 解析描述 + 反问 | 语义 + HITL | **tool**（`ask_clarification` 内置） | 解析是 LLM 自身的活；"版本不猜/描述不足先问"红线需一个能打断并回灌用户答复的工具来 gate |
| 2a 候选召回 bill_match | 向量检索、schema 稳定 | **MCP** | 横切共享底座（算量/审图/FM 都查清单库）；带 Milvus+embedding 重依赖，不塞进 agent 沙箱；按 spec 隔离、独立运维 |
| 2b 在候选内选码 | 受约束的语义分类 | **agent 推理 + 代码兜底** | LLM 在候选内选；"不造码/低置信转人工/空候选转人工"在代码强制（`cost/selection.py`），非工具 |
| 3 套定额 | 语义匹配（仍有歧义） | **MCP**（取候选）+ agent 判别 | 定额库是共享数据原语；歧义消解交 LLM |
| 4 工料机含量 | 查表、确定 | **MCP** | 纯数据访问原语，确定性，多消费方复用 |
| 5 取单价（信息价） | 查表、确定、地区/时效相关 | **MCP** | 同上；缺价标 `no_source` 在服务端确定性执行 |
| 6 综合单价 | 确定性公式（**算钱**） | **tool**（强校验） | 动钱最需 `args_schema` 闸门：输入须已校验数值、结果唯一、**绝不容 LLM 介入**（= P2 `cost/pricing.py`） |
| 7 汇总出造价 | 确定性公式 | **tool / 服务** | 同 6，纯公式（远期） |

### 四条设计原则（来自架构讨论）

1. **原语一律独立可调（MCP first-class）**：用户请求天然有不同粒度——"只查这个码的信息价"/"只选码"/"只查含量"/"端到
   端组价"。原语按**用户能理解的操作边界**切，每个独立成 MCP 工具：既服务中间步请求，又为将来换强模型自由编排留口。
2. **端到端组价是"并列的复合便利入口"，不是 chokepoint**：复合入口内部确定性地串原语，但**不把原语藏在它后面**。
   中间步请求直接打对应原语、不经过复合入口；端到端请求才走复合入口。
3. **红线下沉到原语边界，不只待在编排层**：一旦允许直接打原语（必须允许），编排层就不再是唯一守门人。
   `spec` 必填 / 不造码 / 不杜撰价 / 算钱要校验，**必须写进每个原语的 schema + 服务端检查**，与调用路径、模型强弱无关。
   若红线只写在复合 skill 里，用户直接打 `price` 原语即绕过全部红线。
4. **能力分级（capability-graded）**：弱模型走复合入口（一次调用）；强模型可直接用原语 + 自身推理应对新组合。
   两种"确定性"要分开——**正确/安全的确定性**（算钱公式、不造码、不杜撰、版本 gating）**永久锁在代码**，再聪明的模型
   也不放权；**可靠性的确定性**（把多步写死成一条调用序列）是**临时拐杖**，随模型变强而软化（让模型自己编排），
   但复合入口留作黄金路径基线 / 回归基准，不删。

### 目标架构

```
            ┌──────── 都暴露给 agent，按请求粒度自选 ────────────┐
  agent ──▶ │  cost_compose(复合)   bill_match   quota   price   │  + ask_clarification(内置 tool)
            └──────┬──────────────────────────────────────────────┘  + compute_unit_price(tool, 算钱)
                   │ 复合入口内部 = 确定性串原语（非 chokepoint）
                   ▼
        bill_match → select_code(LLM+代码兜底) → price_compose → [compute_unit_price]
                   │
        ┌──────────┴──────────────┬─────────────────────┐
     MCP: bill_match          MCP: quota            MCP: price       ← 知识层 :8100 共享原语
     (清单候选库)              (定额子目+含量)        (含量⋈信息价)        红线在原语边界自带护栏
```

### 实现清单（按层）

- **知识层 ce-code（MCP 原语，共享底座）**：`bill_match` / `quota_lookup`(/quota) / `price_compose` 三原语在现有
  :8100 HTTP 之外加 MCP façade，红线落原语边界——详见 `../ce-code/DEV.md §7`。
- **任务层 ce-services**：
  - **tool `compute_unit_price`**（综合单价，确定性，schema 校验）= P2 `cost/pricing.py`。动钱，**绝不入 LLM 链路**，
    `args_schema` 强制数值已校验；人材机费 →（`fee_rate` + `price_composition`）→ 综合单价 → 含税造价。
  - **tool `rollup_cost`**（分部分项→措施→其他→规费→税金 汇总，确定性）= 远期。
  - **复合入口 `cost_compose`**（现 `/cost/compose` + `cost-agent` skill）= **黄金路径快捷件，非 chokepoint**：
    内部确定性串 `bill_match → select_code → price_compose →（P2 后）compute_unit_price`。
  - **`select_code`** 维持"LLM 在候选内选 + 代码侧确定性兜底"（`cost/selection.py`），是复合入口**内部环节**，
    不单独暴露给用户（强模型时代可由模型拿候选内联选码）。
- **agent 层**：`ask_clarification`（内置 tool）承接第 1 步版本/描述红线反问（已在 cost-agent agent 放开）。

> 与现状的关系：`/cost/compose` 编排（`cost/orchestration.py`）= 复合入口，已就位；`cost-agent` skill = 其 agent
> 门面，已就位。本方案的**新增工作量** = ① 知识层三原语加 MCP façade（红线复述进 schema）；② 任务层 P2 把
> `compute_unit_price` 做成带 schema 校验的确定性 tool。两者落地后即满足"原语 first-class + 复合并列 + 红线在边界"。

## 任务层能力 MCP façade + 前端依据渲染（决策，2026-06-30）

> 背景：deer-flow 前端「中间过程」折叠流（`message-group.tsx`）只渲染 **思考 + 工具调用** 两类 step；
> 且泛型工具分支只画一行 label、**不渲染工具结果**。`norm-qa` / `cost-agent` 两个 skill 是经 **bash** 跑 Python
> 客户端进来的，bash 分支只显示命令文本、不显示 stdout——于是选码/置信度/cited_clauses 等**依据全埋在 bash
> 输出里看不见**，造价用户在对话里拿不到「凭什么这么答」。这违背「过程信任：让依据可见」的产品目标。

**方案（选 A：MCP 工具化，弃 B：在 bash 分支 sniff 命令）**：tool 与 MCP 对模型是同一个 function-calling 表面，
但 MCP 让**工具名/入参/结果天然结构化**——前端可按**稳定工具名**派发渲染，而非脆弱地解析 bash 命令文本。

- **服务端**：`common/mcp_server.py` 起 FastMCP `ce-task`（streamable-HTTP），把两个**无状态、一把出结果**的任务层
  能力包成 MCP 工具，**复用编排内核、不反代自家 REST**：
  - `norm_qa` → `knowledge_client.search` + `norm/generation.answer`（零召回拒答、不编造，与 `/norm/qa` 一致）；
  - `cost_compose` → `cost/orchestration.compose`（选不出码转 HITL、缺价 no_source、2013 未就绪，红线如实透传）。
  - **HITL 可中断组价会话不在此暴露**：那是有状态 + 交互式，已由前端内嵌 `cost-hitl` marker 卡片驱动（见
    `cost/router.py` 的 session 端点 + `frontend/.../cost/`）。MCP 这层只收**无状态**能力。
  - 挂载：`main.py` lifespan 跑 `task_mcp.session_manager.run()` + `app.mount("/", …streamable_http_app())`，对外
    `:8101/mcp`（与知识层 `:8100/mcp` 同款）。依赖 `mcp>=1.2`（服务器 `uv add mcp`，本地不提交 uv.lock）。
  - 注册：`extensions_config.json` mcpServers 加 `ce-task`（工具名带 server 前缀 → agent 见 `ce-task_norm_qa` /
    `ce-task_cost_compose`）。与知识层 `ce-cost`（bill_match / quota_lookup / price_compose）分工：`ce-cost`=纯数据
    原语，`ce-task`=带 LLM 编排的任务层能力。
- **前端**：`frontend/src/components/workspace/messages/ce-tool-result.tsx` 集中所有造价 MCP 工具的依据渲染
  （`ce-task_*` + `ce-cost_*`），`message-group.tsx` 只加一个 `isCeTool(name)` 委派分支——**不把造价业务字段塞进
  上游通用组件**。按工具名渲染：规范问答→cited_clauses（标准号+条文号）；组价选码→选中码+置信度+转人工+取数状态；
  清单召回→Top 候选；组价取数→定额子目；定额直取→子目+工料机条数。结果形状做防御性读取（MCP 序列化/加载中/异常
  透传都不崩）。
- **bash skill 保留**：`norm-qa` / `cost-agent` 两个 skill 不删，作命令行兜底 / curl 调试；但**对话主路径走 MCP**。

> 验证：前端真跑在服务器，`pnpm check`（eslint+tsc）+ 浏览器对话调 `ce-task_*` 看依据渲染须在服务器过一轮（本地无
> node_modules）。服务端 `mcp` 装好后 `:8101/mcp` 可由 `curl` 列工具校验。
