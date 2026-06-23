# ce-services（任务层）· 进度 TODO

> 任务层（生成 + 编排）执行进度。需求/设计见同目录 `PRD.md`；知识层进度见 `../ce-code/TODO.md`。
>
> **2026-06-18 重构方向**：项目聚焦**深圳房建组价**。知识层 ce-code 已收窄为组价知识库、移除规范条文检索
> RAG（防火轨停做）。任务层随之收敛——**CostAgent（选码 + 组价）为唯一主线**；规范 RAG 的消费方
> `qa/`（/qa）与 `compliance/`（/compliance）**退役**（后端 /search /clause 已删）。
>
> **2026-06-23 双主线并进（当前状态）**：任务层两条产品线均已流程打通，共进程 :8101、共用知识服务 :8100。
> - **Norm-QA（造价规范问答）**：A1–A4 + B1–B2 端到端跑通（✅），**B4 已封装为 deer-flow agent + skill
>   （norm-qa，放开 ask_clarification 缺版本反问，代码就位待服务器验）**；余检索质量调优 + B3 评测（follow-up）。
> - **CostAgent P1（选码闭环）**：Step 0–3 + 5 完成、端到端跑通（✅）；**Step 4 选码评测脚本就位，prompt 现浇/
>   预制消歧服务器重测完成（2026-06-23）：Top-1 70→80%、候选内 78→89%、高置信错码 3→2，矩形柱已救回**。
>   余：扩 gold 稳数字 + 残留 2 错码（矩形梁=召回缺口归知识层 / 过梁=选码细粒度）+ **置信度全 0.95 无区分度（治本）**。
>
> Norm-QA 是从 git 历史**恢复+适配**被删的 hybrid 检索引擎接造价类规范语料（GB 50500/50854/50856、深圳费率/
> 消耗量标准），与 §2.1「去 RAG」无关（非防火）；ce-code 侧进度见 `../ce-code/TODO.md` 同名章节。

---

## 主线一：造价规范问答 Norm-QA —— ✅ 流程打通（质量调优 follow-up）

> 构件/计量计价类自然语言问题 → 造价规范条文检索 → Qwen3 带引用作答（强制引用/强条区分/无依据拒答）。
> 语料已切块就绪（`ce-code/data/structured/chunks/` 9 文档）；旧引擎 + 退役 qa 生成器均可从 git 恢复。

### 任务层 ce-services（生成）
- [x] **B1 `norm/generation.py`**（✅ 2026-06-22）：造价规范问答生成器——承旧 qa 硬约束（强制引用/
  无依据拒答/不编造），但**去强条二分**（/search 无 is_mandatory，强条降级 modal 不落响应），改"忠实引用 +
  条文自带强制性字样则照标"；接 `common/llm.py:call_qwen3`；恢复 `common/knowledge_client.py`（search/expand/clause）。
- [x] **B2 `norm/router.py` + 挂 main.py`**（✅ 2026-06-22）：`POST /norm/qa {query, standard, top_k}`——
  `knowledge_client.search`（打 :8100 /search）→ `generation.answer`；零召回不喂空上下文直返"无依据"；
  异常映射（知识服务 400/503 透传、LLM 不可达 502）。main.py 挂 norm 路由、`/health.routes=["/norm/qa"]`。
- [x] **端到端验证**（✅ 2026-06-22）：服务器 :8100 知识服务 + :8101 任务服务跑通 `POST /norm/qa`——
  检索 177ms → Qwen3 生成 4.3s → 结构化带引用回答（cited_clauses 只引检索到的、不杜撰、带免责声明）。
  A1–A4 + B1–B2 全链路验证完毕。
- [x] **B4 封装为 deer-flow agent + skill**（✅ 2026-06-23，代码就位待服务器验）：三件套对标退役的
  code-qa——`skills/public/norm-qa/{SKILL.md,qa.py}`（纯 stdlib 薄 HTTP 客户端，默认打 :8101 /norm/qa、
  `--no-generate` 打 :8100 /search；`--standard` 必填无默认 + 客户端版本红线拦非法代号）；
  `extensions_config.json` 启用 `norm-qa`；`config.yaml` 注册 `subagents.custom_agents.norm-qa`
  （**放开 `ask_clarification`**——用户没说规范版本时反问澄清，实现多轮"缺 spec 追问"，这是比单轮端点更进一步的智能体闭环）。
  本地：qa.py py_compile + 必填/版本红线行为冒烟过；JSON 合法；YAML 结构对齐 compliance-checker 模板（本地无 PyYAML，
  最终解析待服务器）。**待服务器加载 agent + 走一轮多轮问答验证。**
- [ ] **B3 评测**：`ce-code/data/eval_set` 建造价规范问答评测集（条文召回 + 引用准确率）。
  ⚠️ **前置瓶颈=检索质量**：实测计量规则在附录表格里、未进嵌入 → 召回偏弱（见 `../ce-code/TODO.md` 四）。
  建议先做表格内容增强再评测，否则评的是被表格缺失拖累的下限。

> 知识层检索引擎恢复（A1–A4）见 `../ce-code/TODO.md`。

---

## 主线二：CostAgent —— 构件 → 选码 → 组价（🟢 P1 选码闭环端到端打通，余 Step 4 评测）

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
- [x] **`cost/selection.py`**（✅ 2026-06-22，本地兜底逻辑验证）：`select_code(description, candidates, llm_url, model_id)`
  → `{code, confidence, reason, need_review, alternatives}`；系统提示钉死三红线 + **代码侧确定性兜底双保险**：
  ① code∉candidates（造码）→ 作废 null + need_review + 标注；② confidence<0.6 → 强制 need_review；
  ③ 空候选 → 不调 LLM 直接转人工；alternatives 仅留候选内 code。Qwen3-8B 直出 JSON（复用 `common/llm`）。
  本地打桩测 5 路径（正常/造码/低置信/自报 review/空候选）全过。
- [x] **`cost/orchestration.py`**（✅ 2026-06-22，本地四路径验证）：`compose(description, spec, region, llm_url, model_id, top_k)`
  串 `bill_match → select_code → price_compose`；选不出码（code=None）→ 跳过组价、`price_status=skipped(need_review)`；
  spec=2013 组价未就绪 → 捕获 compose 501 **降级**为 `price=None` + `price_status` 说明（保留选码结果）；
  bill_match spec 400 / 知识服务 503 / compose 404·503 不吞、上抛由 router 映射。
- [x] **`cost/router.py` + 挂 main.py**（✅ 2026-06-22）：`POST /cost/compose {description, spec, region, top_k}`
  → `orchestration.compose`；异常映射（依赖 HTTPError 透传状态码 / 不可达 503 / LLM 非法 JSON 502）；
  need_review、price_status 原样冒泡供 HITL。main.py 挂 cost+norm 双路由、`/health.routes=["/norm/qa","/cost/compose"]`。
  （不单建 `server.py`——沿用 norm 模式，main.py 唯一入口。）
- [~] **Step 4 选码评测**（脚本就位待服务器跑，2026-06-22）：`tools/eval_select.py` 复用 ce-code `match_gold.jsonl`，
  对每条 `bill_match` 召回 → `select_code` 选码，量三指标：**Top-1（端到端）**= 选中码==金标（PRD §6 红线 ≥85%）、
  **候选内 Top-1**= 仅在召回到正解子集上算（隔离召回拖累、纯量选码）、**自动定稿准确率**= need_review=false 时的命中率
  + **高置信错码**计数（绕过 HITL 的危险案例，须为 0）。纯指标函数本地打桩 5 路径全过；待服务器 :8100+vLLM 跑实测。
  ⚠️ gold 仅 10 条统计意义弱，稳数字需先扩 gold（同待办 A「评测集扩充」）。
  跑：`python -m tools.eval_select --spec 2024 --top-k 10`（从 ce-services 根）。
  - **首轮实测（2026-06-22→23）**：修 gold 现浇/预制错标后 Top-1=70%、候选内 78%、高置信错码 3 条。
    三条错因不同：①现浇矩形柱选了预制 010503001（选码层，已修见下）；②现浇矩形梁未召回（钢筋混凝土梁
    名实不符，**召回缺口归知识层**）；③现浇过梁选了零星现浇构件（细粒度语义）。
- [x] **选码 prompt 补现浇/预制消歧**（✅ 2026-06-23，服务器重测完成）：实测暴露 LLM 只看名称被「矩形柱」等
  字面精确命中诱选预制码，根因=`build_user_message` 没把候选的 `cast_type`（建库期从 caption 派生「预制/装配」）
  喂给 LLM、chapter 又现浇预制同名。修：① user message 候选增 `浇筑方式=预制/装配`（仅标注 cast_type 命中者，
  现浇/非混凝土不贴标）；② SYSTEM_PROMPT 加铁律 5——查询未明示「预制/装配」一律选现浇、禁因名称字面相同选预制。
  - **重测实测（2026-06-23，n=10 spec=2024 top_k=10 rerank=off）**：**Top-1 70→80%、候选内 78→89%、
    高置信错码 3→2、Recall@10=90%**。① 现浇矩形柱 ✅ 已救回（010502006）；② 现浇矩形梁仍 ✗（金标
    010502011 **未召回**，LLM 在无正解候选里高置信选 010502025 → **召回缺口归知识层 bill/match**）；
    ③ 现浇过梁仍 ✗（金标 010502023 **已召回**但选了相邻 010502025，零星/过梁细粒度语义 → **选码层未覆盖**）。
- [ ] **⚠️ 置信度全 0.95 无区分度（红线可达性·治本）**：重测 10 条 confidence **全为 0.95**，
  代码侧 `confidence<0.6 强制 review` 兜底从未触发、HITL 安全网形同虚设；"高置信错码须为 0" 在 LLM 永远
  自报 0.95 下**结构性难达**。需让 LLM 在候选都不够贴切时真降置信，或代码侧用 rerank/语义距离辅助置信校准。
  **前置=扩 gold 到 30–50 条**（n=10 无统计依据，校准会过拟合）。
- [x] **端到端验证**（✅ 2026-06-22，服务器跑通）：`curl /cost/compose {"description":"C30现浇矩形柱","spec":"2024","region":"深圳"}`
  → 选码 `010503001 矩形柱`（confidence 0.95、need_review false、reason 有据）+ 组价取数（2 条模板定额 + 工料机含量
  + 信息价 matched/no_source）。三红线守住（高置信不 review、no_source 不杜撰）。**P1 选码闭环端到端打通。**
  - 观察（归知识层 follow-up）：召回定额是矩形柱**模板**（措施），柱本体混凝土定额另算——bill→quota 映射取数行为，不影响选码。

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
