# ce-services（任务层）· 进度 TODO

> 任务层（生成 + 编排）执行进度。需求/设计见同目录 `PRD.md`；知识层进度见 `../ce-code/TODO.md`。
>
> **2026-06-18 重构方向**：项目聚焦**深圳房建组价**。知识层 ce-code 已收窄为组价知识库、移除规范条文检索
> RAG（防火轨停做）。任务层随之收敛——**CostAgent（选码 + 组价）为唯一主线**；规范 RAG 的消费方
> `qa/`（/qa）与 `compliance/`（/compliance）**退役**（后端 /search /clause 已删）。

---

## 主线：CostAgent —— 构件 → 选码 → 组价（🟡 P1 进行中）

> 知识层只召回候选 + 取数（Recall@10=60%，正解已进 top-k）；CostAgent 在候选内 **LLM 选码** + **确定性组价**
> + HITL 红线——Top-1 选码本就归任务层（PRD §6）。端到端：
> **构件描述 → bill_match 候选 → LLM 选码 → price_compose 组价**。

### 已就位
- [x] **造价取数客户端 `common/cost_client.py`**（✅ 2026-06-17，+ 2026-06-18 加 `spec` 必填透传）：
  `bill_match(query, spec)` / `price_compose(region, code, spec)` / `quota(region, code)`，复用 `KNOWLEDGE_URL`:8100，
  path 段 `quote` 编码避中文 404。**CostAgent 调用前须向用户确认国标版本（2013/2024）再传 spec。**

### P1 —— 选码闭环（HTTP 端点，当前任务）
> 决策（2026-06-18）：P1 只做选码闭环（不组装综合单价）；先 HTTP 端点（不先 deer-flow agent）；退役 qa/compliance。

- [x] **退役 qa/ + compliance/**（✅ 2026-06-22）：`git rm` 掉 qa/ compliance/ common/knowledge_client.py
  + skills/public/{code-qa,compliance-check}；`config.yaml` 去 compliance-checker 自定义 agent、
  `extensions_config.json` 去两 skill 条目；`main.py` 去两路由、`/health.routes=[]` 待挂 cost；
  `call_qwen3` 从 `qa/generation` 抽到 `common/llm.py` 供选码复用；README 重写为 CostAgent 主线。
- [ ] **`cost/selection.py`**：LLM 选码 `select_code(description, candidates)` → `{code, confidence, reason, need_review, alternatives}`；
  系统提示钉死「只能从候选里选、不许造码、不确定标 need_review」；Qwen3-8B 直出 JSON（非 function-calling）。
- [ ] **`cost/orchestration.py`**：`bill_match(spec)` → `select_code` → `price_compose(region,code,spec)` 组装；2013→compose 501 透传「未就绪」。
- [ ] **`cost/router.py` + `server.py`**：`POST /cost/compose {description, spec, region, top_k}`；异常→状态码（spec 错 400 / 知识服务不可达 503）。`main.py` 挂载、`/health` 更新。
- [ ] **选码评测**：复用 ce-code `match_gold`，量 **LLM 从 top-k 选中正解的 Top-1**（PRD §6 业务层红线 ≥85%）——把知识层 Recall@10=60% 接到任务层 Top-1，端到端可量化。
- [ ] **端到端验证**：起 :8101 → `curl /cost/compose` → 选码 + 工料机 + 价（带 no_source）跑通。

**红线**：选码 `need_review`（低置信→HITL，只建议不定稿）/ `no_source` 不杜撰、透传缺口 / `spec` 必填 / **P1 不算钱**。

### P2 —— 综合单价组装（⬜ 待办）
- [ ] `cost/pricing.py`：人材机费 →（`fee_rate` 管理费/利润/风险 + `price_composition` 构成规则）→ 综合单价 → 含税造价，**确定性公式**（LLM 不算钱）。

### P3 —— deer-flow agent + skill（⬜ 待办）
- [ ] 注册 `cost-agent` + skill：多轮追问（缺 spec / 构件描述时问用户），把端点包成可编排复用能力（同 code-qa/compliance-check 旧模式）。

---

## 退役 · 规范 RAG 消费方（⛔ 2026-06-18，后端已随 ce-code 移除）

- [⛔] **阶段1 code-qa skill**（原 `/qa`，Qwen3 结构化生成 + 强制引用）——后端 :8100 `/search` 已删。
- [⛔] **阶段2 项目级合规审查**（原 `/compliance`，参数提取→并行检索→维度判定→反思）——后端 `/search /clause` 已删。
- 历史（skill HTTP 服务化 / v3 任务层迁出知识层 / Docker / 单进程合并）随 git 追溯，不再展开。
  规范条文检索能力日后按需重建时，再评估是否复活相关编排。

## 设计辅助（⬜ 远期，依赖规范检索重建）
- [ ] sandbox 执行规范计算公式（疏散宽度/防火间距等）——依赖规范条文检索能力（当前已移除），重建后再议。
