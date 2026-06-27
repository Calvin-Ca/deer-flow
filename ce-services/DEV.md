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

---

## 开发环境要点（任务层专属）

- **独立 uv 项目**：依赖极轻（仅 `fastapi`/`uvicorn`/`requests`/`pydantic`），首次 `cd ce-services && uv sync`
- **不依赖 GPU / torch / Milvus 客户端**：镜像可极轻量（Docker tasks 镜像 ~200MB）
- **包管理**：`uv add` 管理依赖，**严禁 `uv pip install`** 绕过 `pyproject.toml`

> 共享环境基础（服务器路径、Python 版本、uv 版本）见根 `CLAUDE.md` §2.3。

---

## 起服务

任务服务（:8101，`/qa` + `/compliance` 共进程）启动命令（含 Docker 全栈）见 `README.md`。**前置：知识服务 :8100 必须先起。**

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
