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
>   预制消歧服务器重测完成（2026-06-23）：Top-1 70→80%、候选内 78→89%、高置信错码 3→2，矩形柱已救回**；
>   **P3 已封装 deer-flow agent + skill（cost-agent，缺 spec/描述反问 + HITL 红线，注册层闭环待服务器验）**。
>   余：扩 gold 稳数字 + 残留 2 错码（矩形梁=召回缺口归知识层 / 过梁=选码细粒度）+ **置信度全 0.95 无区分度（治本）**。
> - **两 agent 编排层共同待办**：norm-qa / cost-agent 的 HTTP 端点(:8101)均已实测，但 **deer-flow agent 编排层
>   （gateway :8001 加载 custom_agent + qwen-plus 多轮反问）两条都未在服务器跑过**——下一道共同工序。
>   注：agent 基座=qwen-plus(DashScope，function-calling 可靠)，生成/选码=Qwen3-8B :8099，已规避 Qwen3 调 skill 不稳的坑。
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
  ⚠️ **前置瓶颈=检索质量（2026-06-23 已字段级确证）**：实测"矩形柱按什么计量"召回 15 条无现浇柱规则、误引
  装饰柱/钢柱；隔离确证=纯召回缺失（skip_rerank on/off 都捞不到），根因=计量规则困在 chunk `tables` 字段、
  content 只存表标题、检索文本不含表体（见 `../ce-code/TODO.md` 四 🔴 条）。**先修知识层表体注入再做 B3，
  否则评的是被表格缺失拖累的下限。**

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

### P2 —— 综合单价组装（🟢 tool + 端点 + 复合入口接线就位，本地验证；待服务器联调）
- [x] **`cost/pricing.py` `compute_unit_price`**（✅ 2026-06-27，本地验证）：纯确定性算钱器，按 GB 50500
  §2.0.9「综合单价 = 人工费 + 材料费 + 施工机具使用费 + 管理费 + 利润 + 风险费用（不含增值税）」。
  **pydantic 闸门 `UnitPriceInput`**（`extra=forbid`、金额/费率 `ge=0` + `allow_inf_nan=False`、`quantity>0`、
  `fee_base` 必填枚举 labor/labor_machine/lmm）——动钱那步无论谁/哪条路径调用都被这道 schema 拦在边界。
  管理费/利润/风险=**取费基数 × 费率**，Decimal `ROUND_HALF_UP` 逐项量化到分；可选 `tax_rate` → 含税合价。
  本地 4 路径验证：正常+含税、lmm 基数无税、ROUND_HALF_UP（2.345→2.35 非 banker's）、闸门 7 例全拦。
  - **🔴 红线落地（不杜撰动钱）**：费率库 `fee_rate` **只收录**安文措施费/夜间/赶工/总包/增值税/附加税/工程
    保险费，**不含管理费、利润率**；定额 `base_price` 亦为净人材机基价。故管理费/利润率**一律由调用方（HITL）
    按工程类别给定**，工具绝不内置默认；取费基数 `fee_base` 亦显式声明、不按地区猜——本工具不替任何动钱
    数字填默认（填了即杜撰）。**待办**：知识层补抽管理费/利润率表（深圳消耗量标准/费率标准），方可由复合入口自动喂率。
- [x] **暴露为 tool（HTTP 表面）+ 接入复合入口**（✅ 2026-06-27，本地验证）：
  ① 独立端点 `POST /cost/unit-price`（body=`UnitPriceInput`，FastAPI 据 schema 自动 422=pydantic 闸门），
  main.py `/health.routes` 加该路由；② `/cost/compose` 加可选 `rates` 块（管理费/利润/风险率 + 取费基数 + 税率），
  给定则末步 `orchestration._price_unit_prices` 对每条定额按**定额基价**算综合单价（缺基价→`missing_base` 不杜撰），
  缺 `rates` 维持 P1 行为（仅选码 + 取数、不算钱）——复合入口非 chokepoint，原语亦可单独打。本地验证算价 + 缺基价 guard。
- [x] **服务器联调**（✅ 2026-06-27，:8101 实测）：`POST /cost/unit-price` 正常路径数字精确命中
  （unit_price=199.5 / 管理费=13.0 / 利润=6.5 / total=399.0 / tax=35.91 / 含税=434.91）+ 负金额→422 闸门生效。
  （坑：旧实例占 :8101 致新进程 Exit 1 静默失败、health 仍显旧 routes；`lsof -ti:8101 | xargs -r kill` 后重起即好。）
- [ ] **`/cost/compose` 带 rates 末步算价待真验**：实测 C30现浇矩形柱选到 010502025 零星现浇构件、`quota_count=0`
  无映射定额 → 无 quota 可挂 unit_price（选码错 + 该码无定额映射=主线二已知缺口，非 P2）；需换能召回带定额的正确码再看 unit_price 字段（接线逻辑本地已验 + missing_base guard 已验）。
- [ ] **价差精算（follow-up）**：当前综合单价以**定额基价**为人材机口径；信息价价差调整（amount 基）属后续精算，未做。

### 原语 / 复合入口的 tool·MCP 暴露（⬜ 待办，方案见 DEV）
> 方案：`DEV.md`「组价能力对外暴露：skill / tool / MCP 分层方案」+ `../ce-code/DEV.md §7`。
> 原则：原语 first-class（独立可调）/ 复合入口非 chokepoint / 红线下沉原语边界 / 能力分级。
- [x] **知识层三原语加 MCP façade**（✅ 2026-06-28，前端实测调通）：`ce-cost_bill_match` /
  `ce-cost_quota_lookup` / `ce-cost_price_compose` 经 :8100/mcp 暴露，前端对话已能直接调、中间过程可见。
  踩坑链（治本要点，已存记忆 [[project_mcp_tool_exposure]]）：① cost-agent 的 `tools` 是**精确名 allow-list**，
  MCP 注册在 extensions_config.json≠该 agent 可用，须把 `ce-cost_*` 三名加进 `config.yaml` agent 的 `tools`；
  ② 仅放行白名单不够——cost-agent prompt「一切走 cost.py 脚本」会让弱模型(qwen-plus)把 MCP 工具**当脚本 bash 执行**，
  须在 system_prompt 区分「中间步直调工具 / 端到端走 cost.py、且钉死『是工具不是脚本』」；③ 旧 :8100 进程早于 MCP
  commit→/mcp 404，须 `git pull` + `uv sync`(装 mcp>=1.27) + 重启 knowledge_api；④ gateway MCP 缓存按
  extensions_config.json mtime 失效，:8100/mcp 起来后须 `touch extensions_config.json` 逼其重新发现工具。
- [x] **复合入口 `cost_compose` 保持并列、非 chokepoint**（✅ 2026-06-28）：`/cost/compose` + `cost-agent` skill
  就位；前端实测「只要候选别选码」中间步请求已能直接调 `ce-cost_bill_match` 原语、不被迫走复合入口。
- [x] **`compute_unit_price` tool**（✅ 2026-06-27，见上 P2）：已按 tool 形态暴露——独立端点 `POST /cost/unit-price`
  （pydantic 闸门）+ 复合入口 `/cost/compose` 内部可选调用，两路并列、红线（不杜撰费率/取费基数显式）在原语边界自带。

### P3 —— deer-flow agent + skill（🟢 注册层闭环，待服务器验多轮）
- [x] **注册 `cost-agent` + skill**（✅ 2026-06-23，代码就位待服务器验）：对标 norm-qa 三件套——
  `skills/public/cost-agent/{SKILL.md,cost.py}`（纯 stdlib 薄 HTTP 客户端，打 :8101 /cost/compose；
  `--spec` 必填无默认 + 客户端版本红线拦非法 spec）；`extensions_config.json` 启用 `cost-agent`；
  `config.yaml` 注册 `subagents.custom_agents.cost-agent`（**放开 `ask_clarification`**——缺国标版本/
  构件描述不足时反问澄清，多轮闭环；system_prompt 钉死 HITL 红线：need_review 不当定稿 / no_source 不
  编价 / 2013 未就绪只出选码 / 不算钱）。本地：cost.py py_compile + 三红线冒烟过（非法 spec / 缺 spec /
  连接失败）；extensions JSON 合法、config.yaml 缩进对齐 norm-qa（本地无 PyYAML，最终解析待服务器）。
  **待服务器加载 agent + 走一轮多轮组价验证（缺 spec 追问 → 选码 → 组价）。**

---

## 主线三：HITL 可中断组价编排（🟢 全 13 步图打通：编码/定额/信息价/费率/参数/总造价六类闸 + 持久化；待前端接 + 服务器联调后段）

> 设计见 `HITL_DESIGN.md`、开发见 `DEV.md`「HITL 可中断组价图」。判断：13 步组价装不进 `cost.py` 黑盒
> （不能暂停 / 不能发中间态），编排上提成 ce-services 独立 langgraph 图——每数字带 provenance 信封、
> 每介入点是可暂停可恢复闸门。本期 = §9 路径**步1（信封）+ 步3（图骨架）**，curl 驱动无头。

### 本期落地（✅ 2026-06-28，本地 py_compile + 纯函数单测）
- [x] **provenance 信封 + 原语适配器** `cost/provenance.py`：§5.1 `{step,status,result,provenance}` 信封；
  `list_match`（bill_match+select_code 包一层）/ `from_price_compose`（一次取数拆定额块+信息价材料块）。
  **原地包**现有原语不重写；信息价文件名+行号级 `source_ref` 知识层暂未返回，best-effort 填 + 标 `TODO(knowledge-layer)`。
- [x] **任务状态 §5.4** `cost/state.py`：`CostTaskState` TypedDict（events/audit_log/overrides 用 `operator.add` reducer）
  + 纯函数 helper（`lock_value` 钉值 locked=True、`audit_entry`、`override_entry`）。
- [x] **门控 §6 + payload §5.2/5.3** `cost/gates.py`：`should_pause_coding/quota/price`（是否跳闸全在代码、不交弱模型）；
  `confirm_payload`/`input_payload`；`apply_confirm_decision`（approve/select_alternative/manual_override，越界备选回退主值）。
- [x] **可中断图** `cost/graph.py`：`setup→list_match→list_gate→(有码?)compose→quota_gate→price_gate→done`。
  **compute/gate 双拆**——LLM（select_code）放上游 compute 节点跑且仅跑一次，gate 只读 state+interrupt，
  避免 langgraph resume 重跑节点头部导致 LLM 漂移（原则 3）。`done` 前留综合单价/措施/规费/末尾 review 挂点。
- [x] **会话门面 + 端点** `cost/session.py`（图+SqliteSaver 单例，thread_id=task_id）+ `cost/router.py` 三端点：
  `POST /cost/session/start`、`POST /cost/session/{id}/resume`、`GET /cost/session/{id}/state`（懒加载隔离 langgraph 依赖，
  不影响 `/cost/compose` 简单路径）。`main.py` /health.routes、`config.py`（DB 路径 + τ）、`.gitignore`、`pyproject.toml` 同步。
- [x] **本地验证**：8 文件 `py_compile` 过；门控阈值边界 / 决策三动作 / lock/audit/override / payload 结构 21 项纯函数单测全过。

### 服务器联调（🟡 2026-06-28，前半链路实测通过）
- [x] **依赖 + 起服务**：服务器 `uv add langgraph langgraph-checkpoint-sqlite` 成功；:8101 起服务 /health 显三条 session 路由。
  （坑：旧实例占 :8101 致新进程 bind errno 98 静默回滚，`lsof -ti:8101 | xargs -r kill` 后重起即好——同 P2 坑。）
- [x] **start → 编码闸**：`start{C30现浇矩形柱/2024/深圳}` → `awaiting_input`，返回编码 confirm 闸 + provenance 信封。
  **门控实测正确**：confidence=0.95（≥τ 0.75）但因有备选候选 → 命中「多候选并列→停」规则停闸，**没静默放过错码**
  （模型自动挑了 `010502025 零星现浇构件` 错码，被闸门拦下交人工 = HITL 价值印证；选码错本身是选码层已知缺口，非图职责）。
- [x] **编码 approve → compose → 定额闸**：approve 钉码（`code.locked:true`/`by:user`/audit 有 approve 记录/events 累积）；
  compose 对 `010502025` 取数 `quotas=[]`（该错码无定额映射）→ `pick_quota status:need_review` → 定额闸因「无子目」停闸
  = 选错码带出的下游空洞**如实透传**（非图 bug）。
- [x] **修空定额 approve IndexError**（commit e1db6b68）：空 `quotas` 时 `apply_confirm_decision` approve 取 `quotas[0]` 崩溃，
  改为空列表回退 `main_value=None`（不崩、交下游/审计）。本地纯函数复验通过。
- [x] **全链路实测端到端全绿**（✅ 2026-06-28，:8101 实测，会话 bb52418c）：编码闸 `manual_override→010503001`（钉值
  locked/override/audit）→ compose → 定额闸 `approve`（子目 010006-15 钉值）→ 信息价闸：**命中(matched)材料自动过且带数**
  （松杂枋板材 value=1904.0 / 涂胶模板 49.0，source_ref=「信息价 [2026-05-01,2026-06-01)」），**仅 no_source 逐项停**录入。
  终态 `status:done`、materials 20=命中3+录入17+缺0、overrides 18、audit 19，数字自洽。
- [x] **跨进程持久化验证**（✅ 2026-06-28，原则 4）：`lsof kill` 重启 :8101 后读同一 task_id → `status:done`、
  materials 20、audit 19 与重启前一致 = SqliteSaver 落盘生效，HITL 可跨会话恢复。
- [x] **实测修两 bug**：① 空定额 approve IndexError（commit e1db6b68）；② 信息价命中态判定——知识层命中字面量为
  `"matched"` 非 `"ok"`，致命中材料误判 no_source、单价未取（commit ec803d83：`{ok,matched}` 算命中、取 `unit_price` 转
  float、source_ref 用价类+期段）。
- [x] **信息价闸过度提示已修**（✅ 2026-06-28，commit 7114d73b，:8101 实测）：`from_price_compose` 对
  `category!=材料` 或 `unit==%` 的资源标 `status:"from_quota_base"`（价在定额基价、不计闸）；`should_pause_price`
  仅 `no_source`（或命中但单价缺失的 ok）才停。**实测**：缺价闸首停由「技工人工费」改为实物材料「对拉螺栓」；
  终态 materials 20 = from_quota_base:8 + ok(命中):3 + user_input:9，no_source 清零、录入数 17→9。
- [x] **（小）缺价闸 context 补 category/spec/consumption**（✅ 2026-06-28）：`price_gate_node` interrupt context
  补全这几个字段（前端缺价录入卡展示用）。
- [x] **补后续节点全 13 步**（✅ 2026-06-28，本地全图 e2e 验证）：图链路补到
  `price_gate → rates_gate → params_gate → rollup → done`，后段三节点均**确定性算钱、无 LLM**（resume 重跑无漂移，
  故 interrupt 与计算同节点、不必双拆）：
  - **§8 综合单价费率闸 `rates_gate_node`**（挂现成 `compute_unit_price`）：门控 `gates.should_pause_rates`——
    费率块缺管理费率/利润率/取费基数（政策数、库内无）则停闸录入，齐则自动过；钉率后用 `quota_gate` 保留的
    **定额基价**（新增 `item["quota_basis"]`）算综合单价（**不含税**，税金在 rollup 一次性计，GB 50500 §2.0.9）；
    手填/越界子目无基价 → `unit_price.status=missing_base`（不杜撰）。
  - **§10⑪§12 项目级费用闸 `params_gate_node`**：录入措施/其他/规费 + 税金率；门控 `should_pause_params`——
    税金率（政策数）缺则停，措施/其他/规费可缺省 0。本节点只采集、不算钱。
  - **§13 末尾 review `rollup_node`**（新增原语 `pricing.rollup_cost` + `RollupInput` pydantic 闸门）：
    确定性汇总「分部分项(Σ综合合价) + 措施 + 其他 + 规费 →(税前)→ +税金 = 总造价」后**始终暂停**复核
    （§6「末尾 review 始终暂停」），resume(approve) → done；缺综合单价的 item 计 `missing_unit_price_items`、不计金额。
  - 状态扩 `params`/`rollup` 字段；`session._format` 透出 `rates`/`params`/`rollup`。
  - **本地验证**：8 文件 py_compile 过；新增纯函数单测 22 项（rollup_cost 数字/HALF_UP/闸门 + 费率参数门控 +
    _unit_price_for + _compute_rollup）+ **全图 e2e 冒烟 14 项**（monkeypatch 取数原语，验 price→rates→params→rollup
    四闸 interrupt/resume 链路 + 总造价数字自洽 515.03 + 审计/override 链全）全过。**待服务器真链路联调。**
- [ ] **接前端**：依据卡（events provenance）+ 两类控件（confirm/input payload）——本期无头，前端后续接。
- [ ] **知识层补 `source_ref`**：信息价期号+行号 / 定额库号 / 清单条文号回填原语返回（设计 §9 步1 后续，去 `TODO(knowledge-layer)` 标记）。
- [ ] **门控阈值调参**：τ 从保守（多停）逐步放松（§6）。
- [ ] **接 agent（远期）**：lead_agent/cost-agent 只做意图路由触发会话，不当逐步编排器（§1.2）。

**红线**：弱模型不驱动流程（跳闸判断在图代码）/ provenance 是字段不口述 / override 钉值不重跑 LLM / 缺口（no_source/need_review/501）如实透传不杜撰。

---

## 退役 · 规范 RAG 消费方（⛔ 2026-06-18，后端已随 ce-code 移除）

- [⛔] **阶段1 code-qa skill**（原 `/qa`，Qwen3 结构化生成 + 强制引用）——后端 :8100 `/search` 已删。
- [⛔] **阶段2 项目级合规审查**（原 `/compliance`，参数提取→并行检索→维度判定→反思）——后端 `/search /clause` 已删。
- 历史（skill HTTP 服务化 / v3 任务层迁出知识层 / Docker / 单进程合并）随 git 追溯，不再展开。
  规范条文检索能力日后按需重建时，再评估是否复活相关编排。

## 设计辅助（⬜ 远期，依赖规范检索重建）
- [ ] sandbox 执行规范计算公式（疏散宽度/防火间距等）——依赖规范条文检索能力（当前已移除），重建后再议。
