# ce-services（任务层）· 需求与设计 PRD

> 任务层 = **生成 + 编排**，是知识服务（:8100）的**纯 HTTP 客户端**。本文件是 `ce-services/` 的需求/设计上下文，改这一层代码前先读。
> 项目级共享上下文见根 `CLAUDE.md`；知识层见 `../ce-code/PRD.md`；进度见同目录 `TODO.md`。

任务层职责：打知识服务 `/search` 拿裸条款，再叠加各自的任务逻辑（问答生成 / 合规编排）。**不 import retrieval、不连 Milvus / 向量库。** 当前两个任务合并在单一进程 `main.py`（:8101），`/qa` + `/compliance` 共端口。

**为什么生成/编排留 server 端、不下放自由 agent**：强制引用 / 强条区分 / 无依据拒答 / 漏强条=事故，这些是硬约束，必须**确定可复现**，不交给自由 agent 推理。

**目标用户**：通用咨询（设计师、施工、监理、公众均可使用）；回答需"准确但易懂"，避免行话。

**风险与红线（输出侧）**：

| 风险 | 应对 |
|---|---|
| 法律责任 | 全链路免责声明 + 不替代专业审查（见 §1 输出硬性约束） |
| 编造条文 | 任何事实必须带条文号引用，无依据则拒答 |

---

## 1. 生成层 — 强制引用 + 可追溯（`/qa`）

**结构化输出格式**（`qa/generation.py`，Qwen3-8B vLLM）：

```json
{
  "answer": "自然语言回答（面向通用用户，避免行话）",
  "applicable_clauses": [
    {
      "standard": "GB 50016-2014(2018)",
      "clause": "5.3.4",
      "text": "原文条款全文",
      "is_mandatory": true,
      "relevance": "direct"
    }
  ],
  "referenced_clauses": [...],
  "uncertain_aspects": ["需要人工核实的点"],
  "out_of_scope_warnings": ["用户场景可能不在本规范覆盖范围内的提示"]
}
```

**System prompt 必须包含**：
1. 必须引用条文号，**无依据则拒答**，不得编造
2. 显式区分"必须 / 应 / 不应 / 严禁"（强制）和"宜 / 可"（推荐）
3. 用户场景与规范适用范围不符时显式告知
4. 多规范冲突时显式呈现差异，不擅自选边

**输出硬性约束**：
- 任何事实必须带条文号引用，否则拒答
- 强条和推荐性条款必须显式区分（不能合并陈述）
- 不确定 → 显式 `uncertain_aspects`，不要编造
- 所有对外回答必须含免责声明："仅供参考，不替代专业审查"

---

## 2. 合规审查编排（`/compliance`）

项目级合规检查是一条**多步确定性流水线**，漏强条=事故，必须 server 端可控：

```
项目自由文本
  ↓ params.py     参数提取（Qwen3 → 类型/高度/面积/用途/建筑类别）
  ↓ queries.py    查询矩阵（规则驱动按合规维度展开 8-16 个检索查询；高层/地下/特殊用途条件触发）
  ↓ orchestration.py
      并行检索（HTTP 打知识服务 /search）
      → 按维度串行去重判定（Qwen3）
      → 反思校验（捕获遗漏维度）
  → 合规报告（适用强条 + 逐维度判定）
```

**关键设计**：`orchestration.py` 的检索经 `common/knowledge_client.py` 打 :8100 `/search`（HTTP），**不进程内 import retrieval**。`/search` 内部 `bm25_top_k = vector_top_k = top_k*2`，与历史进程内直调参数（`top_k=15, bm25/vector_top_k=30, skip_rerank=True`）逐字一致 → 行为等价。

---

## 3. Agent 层 — deer-flow 集成

| 阶段 | 形态 | deer-flow 介入度 |
|---|---|---|
| 条文检索（问答） | 单轮 RAG，封装为 skill | 低（但 skill 机制让它可被其他 agent 复用） |
| 项目级合规审查 | sub-agent：参数提取 → 多轮并行检索 → 综合判定 → 反思校验 | **高——这是 deer-flow 的核心价值** |
| 设计辅助 | + sandbox 执行规范计算公式（疏散宽度、防火间距等） | 利用 deer-flow 的 sandbox |

- `compliance-checker` 注册为 `config.yaml` 自定义 agent
- `compliance-check` skill 含多轮对话编排指令（参数收集 → `task()` 派发 → 报告呈现 → 追问）
- deer-flow 模型切本地 Qwen3-8B（vLLM localhost:8099，全链路无需 OpenAI API）

---

## 4. 任务服务端点（:8101，`main.py`）

qa + compliance 合并单进程，共用端口：

```
POST /qa          = search + 结构化生成（code-qa skill 后端）
POST /compliance  = 参数提取→并行检索→逐维度判定→反思（compliance-check skill 后端）
GET  /health      返回 service:"tasks", routes:["/qa","/compliance"]
```

**代码组织**（`main.py` 用 `include_router` 组装）：
```
ce-services/
├── main.py                 统一入口 :8101，挂载 qa + compliance 路由
├── common/
│   ├── config.py           LLM_URL / LLM_MODEL_ID / KNOWLEDGE_URL（env 可覆盖）
│   └── knowledge_client.py 知识服务 HTTP 客户端：search / expand / get_clause
├── qa/
│   ├── router.py           /qa 端点逻辑
│   ├── server.py           独立启动入口（单独测试用）
│   └── generation.py       检索结果 → 结构化回答
└── compliance/
    ├── router.py           /compliance 端点逻辑
    ├── server.py           独立启动入口（单独测试用）
    ├── orchestration.py    端到端流水线（检索经 knowledge_client.search）
    ├── params.py           自由文本 → 结构化建筑参数
    └── queries.py          结构化参数 → 合规检索查询矩阵
```

---

## 5. skill 客户端（沙箱薄客户端，纯 urllib 零依赖）

deer-flow 沙箱只挂载 `skills/`（POC venv/脚本/数据均不可见），故 skill 退化为**纯标准库 urllib HTTP 客户端**：

- `skills/public/code-qa/qa.py`：默认打任务服务 `:8101 /qa`；`--no-generate` 打知识服务 `:8100 /search` 拿裸条款（给算量/审图复用）
- `skills/public/compliance-check/check.py`：打任务服务 `:8101 /compliance`

响应带 `meta`（各阶段命中数 + 耗时）做前端可观测性；服务端按 `request_id` 打分步日志。

---

## 6. 配置（env 覆盖，`common/config.py`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BCRAG_KNOWLEDGE_URL` | `http://localhost:8100` | 知识服务地址 |
| `BCRAG_LLM_URL` | `http://localhost:8099` | Qwen3-8B vLLM 地址 |
| `BCRAG_LLM_MODEL_ID` | `qwen3-8b` | LLM model_id |

> 文本生成模型：`Qwen3-8B`（vLLM `http://localhost:8099`）。Thinking 切换：user message 末尾 `/think` 启用、`/no_think` 禁用；JSON 输出场景建议 `/no_think`。
