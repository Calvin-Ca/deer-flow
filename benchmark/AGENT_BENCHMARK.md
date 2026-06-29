# 智能体 Benchmark 规范

> 配套文档：`../AGENT_PRD.md`（需求与路由权威源）。本文档定义如何**度量** PRD 中两个 Agent（智能组价 / 规范智能问答）及其依赖能力是否达标。
> 文档定位：把 PRD §8「验收要点」落成**分层、可执行、可复现**的评测体系。
> 单一事实源约定：路由金标准的依据是 `../AGENT_PRD.md` §4.3 决策表；本文档不重定义路由规则，只定义如何测。§4.3 改了，本文档的路由金标准随之更新。

---

## 1. 设计原则

benchmark 的形态由 PRD 的架构特征决定，不是套通用 LLM 评测模板。

| 原则 | 来由（PRD） | 对 benchmark 的含义 |
|---|---|---|
| **分层独立评测** | 三层架构 Orchestrator→Pipeline→ReAct | 路由 / 门控 / 检索 / 生成各自一套指标，再加端到端；某层崩了能定位到层，不被端到端分数掩盖 |
| **确定性层用精确指标** | 路由（§4.3）+ 门控（§4.4）在调 LLM 之前完成，是规则/检索打分 | 这部分用分类准确率/混淆矩阵/误直配率，**100% 可复现、可回归**，不靠人评 |
| **脏请求加权** | §8.1「以脏请求 EH-01~04 正确分流率为主指标」 | 评测集按 EH-01~04 **分层超采样**，不用自然分布（自然分布里干净请求占多数，会掩盖路由缺陷） |
| **红线零容忍** | C-04 检索层不算数 | 不是「越界率低」，是 **越界=0 的 gate**：任何一次 RAG 层算数即判该用例 fail |
| **调参 / 评测隔离** | τ_high/τ_low/w₁/w₂ 可调（§4.4） | 评测集切 dev（调阈值权重）/ test（冻结、只跑一次出数），否则是在测试集上调参作弊 |

---

## 2. 分层 Benchmark 体系（六层）

| 层 | 测什么 | 对应 PRD | 是否机器可判 |
|---|---|---|:---:|
| L1 路由 | 落点分类是否正确 | §4.3 / §8.1 | ✅ |
| L2 置信度门控 | 直配/辅助/ReAct 的分流，误直配率 | §4.4 | ✅ |
| L3 检索 | RAG 召回质量、版本对齐、地域纯净 | C-01/C-02、FR-K | ✅ |
| L4 答案/红线 | 溯源完整、拒答正确、红线零越界 | C-01/C-03/C-04 | ✅（多数） |
| L5 复合拆解 | 复合请求拆得对不对 | EH-01 / FR-X03 | 半自动 |
| L6 端到端 + NFR | 任务成功率、延迟、隔离、可观测 | §8.1/§8.2 | 人评 + 机器 |

### L1 路由 Benchmark（核心）

- **测什么**：给定原始请求，分类器输出的落点是否等于标注落点。
- **现有资产**：[`routing_eval/agent_routing_eval.jsonl`](routing_eval/agent_routing_eval.jsonl)（17 条，说明见 [`routing_eval/README.md`](routing_eval/README.md)）已是一个可用的路由评测集——按 `no_version / with_version / boundary` 分组，量「路由率」+「红线遵守率（不带版本必反问）」，是 L1 与 L4 拒答的现成种子集，扩样时并入本层。
- **数据**：每条样本标 §4.1 四维信号 + 期望落点 + EH 类型，直接对齐 §4.3 决策表七行与 EH-01~04。
- **指标**：
  - 总体路由准确率 **≥95%**（§8.1）
  - **逐行准确率**：§4.3 七行各出一个数，避免「靠多数类刷高总分」；要求**无单行 < 90%**
  - **按 EH 类型召回**：脏请求是主指标，EH-01~04 各单列
  - **混淆矩阵**，重点盯两类危险误分：
    - 「缺特征组价」误判成「高置信直配」——该澄清却直配，最危险
    - 「动态价格」误判成「静态 RAG」——出过期价，违反 C-02 的时效隐含
- **通过线**：总体≥95% 且无单行<90%；两类危险误分方向 ≈ 0。

### L2 置信度门控 Benchmark

依赖检索分数，必须与 L1 分开。

- **前置**：评测时**冻结检索结果**，否则检索波动污染门控指标。
- **标注**：每条组价请求标金标「应直配 / 应 LLM 辅助 / 应 ReAct」（造价专家定）。
- **指标**：
  - **误直配率（最关键，安全指标）**：被判「置信度≥τ_high 直配、绕开 LLM」但实际匹配错的比例。直配绕过 LLM 兜底，错了直接进结果——设极严上限 **≤1%**。
  - 直配覆盖率 / 直配准确率（绕 LLM 路径 precision，§8.1 门控有效性）
  - ReAct 触发召回：该澄清的是否触发 ReAct（漏触发=缺特征硬配）
  - **τ/w 调参曲线**：在 **dev 集**扫 τ_high/τ_low/w₁,w₂，画 (误直配率 vs 直配覆盖率) 权衡曲线，选点后在 test 集验证。
- **通过线**：误直配率 ≤1%；在该约束下直配覆盖率尽量高（作为回归基线，不设硬线）。

### L3 检索 Benchmark（ce-code RAG）

- **现有资产**：检索/匹配确定性底座的纯函数单测已锁住边界行为——[`ce-code/tests/test_bill_match.py`](../ce-code/tests/test_bill_match.py)（清单嵌入文本拼装 + Milvus 命中整形）、[`ce-code/tests/test_bill_quota.py`](../ce-code/tests/test_bill_quota.py)（清单→定额映射 + **国标版本隔离**，直接支撑版本对齐召回 C-01/FR-K05）、[`ce-code/tests/test_splitter_pure.py`](../ce-code/tests/test_splitter_pure.py)（解析/切分，保障入库数据质量上游）。这些是单测不是召回评测，但它们保证了 L3 召回所依赖的确定性逻辑不回归。
- **指标**：
  - Recall@k / MRR：清单项、定额子目、规范条文各算一组
  - **版本对齐召回**（C-01 / FR-K05）：Top-k 命中里版本/年份正确的比例——新旧标准差异类必测
  - **地域纯净度**（C-02）：检索结果非深圳口径泄漏率，应 = 0；EH-03 显式跨省时带正确地域标注
- **通过线**：地域泄漏率 = 0；Recall@k、版本对齐召回设回归基线（随库迭代调）。

### L4 答案 / 红线 Benchmark（机器可判）

- **溯源完整率**（C-01）：FR-K/FR-P 答案附「标准号 + 版本 + 条款/子目」的比例 = **100%**。正则+结构校验机器判，缺版本即不完整。
- **拒答正确性**（C-03）：
  - 拒答率：构造**确知库内无命中**样本，测明确拒答率
  - **误拒率**：构造库内有命中样本，测不该拒而拒的比例（防止「为了不幻觉乱拒」）
  - 合成拒答 F1
- **红线零越界**（C-04）：插桩计算工具 / RAG 层，统计 RAG 层是否发生数值计算，**任何一次 = 该用例 fail**。
- **现有资产**：[`ce-code/tests/test_resource_norm.py`](../ce-code/tests/test_resource_norm.py) 锁住资源归一化的红线行为——单位不可换算时返回 `None`（**不猜价**），是 C-04 在材料标准化环节（FR-P04）的单测护栏；[`routing_eval/agent_routing_eval.jsonl`](routing_eval/agent_routing_eval.jsonl) 的 `boundary` 组（越界应拒答不调脚本）是 C-03 拒答正确性的现成样本。
- **通过线**：溯源完整率 = 100%；红线越界 = 0；拒答 F1 设回归基线。

### L5 复合拆解 Benchmark

路由准确率只看顶层落点，复合请求的难点在**拆得对不对**（FR-X03 结算/变更全靠拆解）。

- **标注**：复合样本标「应拆成哪几个子任务 + 每个子任务的落点」。
- **指标**：子任务集合的 P/R/F1（拆多了/拆漏了/拆错落点）、子任务级路由准确率、汇总正确性。

### L6 端到端 + NFR

- **E2E 任务成功率**：按 FR 类型（FR-P 组价 / FR-K 问答 / FR-C 上下文核对 / FR-X 复合）各一组真实任务，专家用 rubric 打分：结论正确 / 溯源齐 / 无越界 / 无幻觉。
- **NFR（§8.2）**：
  - P95 延迟分类测：FR-K ≤3s、FR-P01 直配 ≤2s、ReAct 单轮 ≤5s
  - 多租户隔离：越权访问用例，跨租户 BOQ/算量不可见
  - 可观测：每次路由落点 / 置信度 / 命中来源是否可审计追溯

---

## 3. 评测集构造

| 维度 | 做法 |
|---|---|
| 分层抽样 | 按 §4.3 七行 + EH-01~04 + FR-K/P/C/I/X **分层配额**，脏请求超采样，**不用自然分布** |
| 规模建议 | 起步：每个落点 ≥50、每个 EH ≥80（脏请求权重高），总量 ~800–1200，可算 95% 附近置信区间 |
| 切分 | **dev**（调 τ/w/检索参数）/ **test**（冻结、只跑一次出验收数）；门控调参只许碰 dev |
| 标注质量 | 造价专家标金标；路由标签**双标 + 仲裁**（路由是主指标，标注噪声直接吃掉 5% 余量） |
| 难例来源 | 真实用户日志里的歧义/缺特征/跨省/复合请求优先入库，比人造样本更能暴露问题 |

### 标注 schema（建议 JSONL，落 `benchmark/retrieval_eval/`）

```jsonc
{
  "id": "RT-0001",
  "query": "C30 现浇矩形柱按特征匹配子目",
  // §4.1 四维信号金标
  "signals": {
    "source": "static",        // static | dynamic
    "context": false,          // 是否需要项目上下文
    "calc": false,             // 是否需要数值计算
    "feature": "complete",     // complete | missing | na
    "intent": "single"         // single | composite
  },
  "expected_route": "组价Agent",     // 期望顶层落点（§4.3）
  "expected_subtasks": [],           // 复合请求：子任务+各自落点（L5）
  "gate_gold": "direct",             // direct | llm_assist | react（L2，仅组价类）
  "expected_sources": [              // C-01 溯源金标
    {"std": "SJG 171", "version": "2024", "clause": "..."}
  ],
  "expect_refuse": false,            // C-03 是否应拒答
  "region": "shenzhen",              // C-02
  "eh_type": null,                   // EH-01..04 | null
  "fr_code": "FR-P01",
  "split": "test"                    // dev | test
}
```

### 与现有评测集的口径关系（两个视角，需统一）

现有 [`routing_eval/agent_routing_eval.jsonl`](routing_eval/agent_routing_eval.jsonl) 的字段（`agent` / `group` / `expect_route` / `expect_clarify` / `gold`）与上面 §3 schema **不同源**，因为二者站在不同层看「路由」：

| | 现有 jsonl | §3 schema |
|---|---|---|
| 视角 | **编排层 skill 路由**：默认 lead agent 在渐进披露下，能否识别并去调 `qa.py`/`cost.py` 脚本（AGENT_INTEGRATION_DEV §0 升级判定门） | **PRD §4.3 落点路由**：四维信号 → 组价/规范/计算/价格取数 落点 |
| 关键字段 | `agent`(norm-qa/cost-agent)、`expect_route`(是否调脚本)、`expect_clarify`(是否反问版本)、`group` | `signals`(四维)、`expected_route`(§4.3 落点)、`gate_gold`、`eh_type` |
| 量什么 | 路由率 + 红线遵守率 | L1 逐行准确率 + EH 召回 |

二者不是矛盾，是**同一请求的两层标注**：编排层「是否调对脚本」是 PRD 落点路由的下游实现。扩样时按下列原则统一，不要并成一张表硬塞：

- **保留为两个视图、共享 `id` 与 `query`**：一条请求同时挂「编排层标注」和「PRD 落点标注」，靠 `id` 关联（现有 jsonl 的 `A*/B*` 号即主键）。
- **字段映射可推导**：`agent=norm-qa → expected_route=规范问答Agent`、`agent=cost-agent → 组价Agent`；`group=boundary → expect_refuse=true / eh_type=EH-02`；`expect_clarify=true → gate_gold=react`（缺版本即缺特征，EH-04）。映射表本身要可机器校验，防两层标注漂移。
- **谁是主**：PRD §4.3 落点是**业务主视角**（L1 主指标），编排层 jsonl 是**实现验证视角**（验「落点对了，脚本也调对了」）。扩样以 §3 schema 为骨架，编排层字段作为附加列挂上。

---

## 4. 与 PRD §8 的映射 + 补缺

§8 已覆盖：路由正确率、溯源完整率、地域纯净度、拒答正确性、红线零越界、门控有效性、NFR——本体系把它们落成可执行的层。

**PRD 未显式提、benchmark 需补的缺口**：

| # | 缺口 | 为什么要补 | 落在 |
|---|---|---|---|
| 1 | **误直配率设独立安全 gate** | §8.1 提了门控有效性但没设线；直配绕 LLM，错了直接进结果，应升级为最严 gate（≤1%） | L2 |
| 2 | **复合拆解子任务级指标** | §8 只测顶层路由，没测拆解质量，而 FR-X03 结算/变更全靠拆解 | L5 |
| 3 | **误拒率** | §8 只测拒答率；只测这个会鼓励「宁可乱拒」，必须配误拒率 | L4 |
| 4 | **dev/test 切分** | §8 提 τ 纳入回归，但没说调参集与验收集隔离，否则测试集上调参 | §3 |
| 5 | **版本对齐召回** | C-01 要版本，§8 溯源完整率只查「有没有附版本」，不查「版本对不对」，FR-K05 需后者 | L3 |

---

## 5. 验收门线汇总

| 指标 | 门线 | 来源 |
|---|---|---|
| 总体路由准确率 | ≥ 95% | §8.1 |
| 单行路由准确率 | 无 < 90% | 本文档 L1 |
| 危险误分（缺特征→直配 / 动态→静态） | ≈ 0 | 本文档 L1 |
| 误直配率 | ≤ 1% | 本文档 L2（补缺 1） |
| 地域口径泄漏率 | = 0 | C-02 |
| 溯源完整率（FR-K/P） | = 100% | C-01 |
| 红线越界次数 | = 0 | C-04 |
| 拒答 F1 / 误拒率 | 回归基线 | C-03 + 补缺 3 |
| P95 延迟 | FR-K≤3s / FR-P01≤2s / ReAct单轮≤5s | §8.2 |

---

## 6. 现有测试资产盘点

项目里已有的测试文件，按其支撑的 benchmark 层归位。两类性质不同：`routing_eval/`(本目录下) 是**评测集**（可直接喂指标），`ce-code/tests/` 是**纯函数单测**（保确定性底座不回归，是评测层的前提）。

| 文件 | 类型 | 内容 | 支撑层 / 约束 |
|---|---|---|---|
| [`routing_eval/agent_routing_eval.jsonl`](routing_eval/agent_routing_eval.jsonl) + [`README.md`](routing_eval/README.md) | 评测集（17 条） | 路由率 + 红线遵守率（不带版本必反问）；分组 no_version/with_version/boundary | **L1 路由** + **L4 拒答（boundary 组 → C-03）** |
| [`ce-code/tests/test_bill_match.py`](../ce-code/tests/test_bill_match.py) | 纯函数单测 | 清单嵌入文本拼装 + Milvus 命中整形 | **L3 检索**（召回前置逻辑） |
| [`ce-code/tests/test_bill_quota.py`](../ce-code/tests/test_bill_quota.py) | 纯函数单测 | 清单→定额映射 + 国标版本隔离 | **L3 版本对齐召回**（C-01 / FR-K05） |
| [`ce-code/tests/test_resource_norm.py`](../ce-code/tests/test_resource_norm.py) | 纯函数单测 | 资源归一化；单位不可换算 → 不猜价 | **L4 红线**（C-04 / FR-P04） |
| [`ce-code/tests/test_splitter_pure.py`](../ce-code/tests/test_splitter_pure.py) | 纯函数单测 | 解析/切分（建树/引用/目录） | **L3 上游数据质量** |

> 缺口（对照 §2 六层）：L2 门控、L5 复合拆解、L6 端到端/NFR **暂无任何测试资产**；L1 仅 17 条、需按 §3 分层扩样到 ~800–1200；L3/L4 有单测护栏但**无召回/答案级评测集**。

## 7. Agent 任务级 Benchmark（outcome 层 · τ-bench / BFCL / RAGAS 范式落地）

§2 的 L1–L4 测的是**「零件对不对」**——路由分类、门控分流、检索召回、溯源格式，全是确定性、单次可复现、对照 PRD §8 验收门线。但这套测不到**「整件事做没做成」**：组价 agent 多轮调工具、自己决策、最终套出的那条定额到底对不对。这一层（原 L6 的实质）必须独立建，范式不照搬通用 LLM 评测，而是借三个 agent 专用 benchmark 的方法论。**定位：L1–L4 是验收 gate，本层是能力 outcome；两者互补，不可互相替代。**

### 7.0 为什么这层要单独建（与确定性层的本质差异）

| | L1–L4 确定性层 | L7 agent 任务层 |
|---|---|---|
| 测的对象 | 单个零件（分类器/检索器/格式校验） | 模型+工具+编排的整个系统在环境里跑一遍 |
| 判定方式 | 对照金标签，精确 | 对照**最终状态**（套对定额没/引对条款没），outcome-based |
| 复现性 | 单次确定、可进 CI | 有随机性（LLM 采样），**必须 pass^k 多跑** |
| 主指标 | 准确率/召回/泄漏率 | 任务成功率 + 一致性 + 违规率 |
| 范式来源 | PRD §8 验收 | τ-bench / BFCL / RAGAS |

### 7.1 三个子层 → 各对标一个范式

| 子层 | 对标范式 | 测谁 | 核心指标 |
|---|---|---|---|
| **L7-A 端到端组价任务** | **τ-bench** | cost agent 整体（多轮调 :8100/:8101 → 收敛出价） | 任务成功率（终态判定）+ **pass^k** + **红线违规率（独立列）** |
| **L7-B 工具调用** | **BFCL** | cost/norm agent 的工具选择与参数填充 | 工具选对率 + 参数填对率 + schema 合法率；**量化前后对比** |
| **L7-C 规范问答忠实度** | **RAGAS** | norm-qa agent（本质是 RAG，不是 agent 多步） | faithfulness + 引用准确率 + context precision/recall + answer relevancy |

三条铁律（直接来自 AGENT_MS 那三题的结论）：

1. **pass^k 而非 pass@k**（L7-A/B）：同一 case 连跑 k 次（建议 k=5）全对才算过，专测 Qwen3-8B function-calling 的**一致性/不漂移**——单次准确率会掩盖"同输入会飘"。这是小模型底座下最该量的东西。
2. **红线违规率独立计分**（L7-A）：把"任务做对"和"有没有违规"拆成两个数，绝不糅进总分。造价错一个数字要担责，违规率是与 C-04 同级的硬 gate（= 0），延续 §2-L4 的"红线零容忍"到 agent 运行态。
3. **规范问答别套 agent 框架**（L7-C）：它最致命的失败是"答得流畅但引错条款号"，是 RAG 忠实度问题，不是 agent 多步能力问题。用 RAGAS 的 grounding 指标量，**不要**塞进 L7-A 的终态判定里。

### 7.2 任务级 case schema（你后续构建数据集的骨架）

与 §3 的请求级 schema 不同——§3 标"一句话请求该路由到哪"，本层标"一个完整任务的初态、目标、工具环境与可程序化判定的终态"。建议落 `benchmark/agent_eval/`，按子层分文件。

**L7-A 端到端组价任务**（`agent_eval/cost_task/*.jsonl`）：

```jsonc
{
  "id": "TASK-COST-0001",
  "user_goal": "为「C30 现浇矩形柱，柱周长1.8m，泵送」组价",   // 多轮起点，可含缺特征
  "init_context": { "spec_version": "深圳2013", "region": "shenzhen" },  // 项目上下文/版本口径
  "tool_env": ["query_bill_8100", "query_norm_8100", "fee_calc"],        // 本 case 暴露的工具
  "terminal_check": {                  // 程序化终态判定（不比文本，比结果）
    "expected_quota": "A4-1",          // 应套定额子目
    "expected_fee_band": [820, 880],   // 综合单价容差区间
    "must_cite": [{"std": "SJG 171", "version": "2013"}]   // 终态必须带的溯源
  },
  "policy": {                          // 红线，违反即 case fail（独立计分）
    "no_rag_calc": true,               // RAG 层不得算数（C-04）
    "no_region_leak": "shenzhen",      // 不得混入非深圳口径（C-02）
    "clarify_if_missing_feature": true // 缺特征必须先 ask 而非硬配（EH-04）
  },
  "pass_k": 5,                         // 连跑次数
  "difficulty": "ambiguous",           // clean | ambiguous | missing_feature | cross_province | composite
  "split": "test"
}
```

**L7-B 工具调用**（`agent_eval/toolcall/*.jsonl`，BFCL 式单步/少步）：

```jsonc
{
  "id": "TC-0001",
  "query": "查深圳2013口径下 C30 现浇矩形柱的定额子目",
  "available_tools": [ /* 工具 schema 列表，含 query_bill_8100 的参数定义 */ ],
  "expected_call": { "tool": "query_bill_8100",
                     "args": { "desc": "C30现浇矩形柱", "spec_version": "深圳2013" } },
  "arg_match": "subset",     // exact | subset | semantic（参数判定口径）
  "quant_variants": ["fp16", "awq-int4"],   // 量化前后都要跑，对比掉点
  "pass_k": 5, "split": "test"
}
```

**L7-C 规范问答忠实度**（`agent_eval/norm_faithful/*.jsonl`，RAGAS 式）：

```jsonc
{
  "id": "QA-0001",
  "question": "防火墙上的门窗洞口有什么规定？",
  "gold_contexts": [ {"std": "GB50016", "version": "2014", "clause": "6.1.5"} ],  // 应检索到的条文
  "gold_answer_points": ["不应设置普通门窗", "确需设置应采用甲级防火门窗"],          // 忠实度比对的事实点
  "ragas_metrics": ["faithfulness", "context_precision", "context_recall",
                    "answer_relevancy", "citation_accuracy"],
  "expect_refuse": false,    // 库内无命中的对照样本设 true，配合 §2-L4 误拒率
  "split": "test"
}
```

### 7.3 评测者与判定（怎么自动判）

- **L7-A 终态**：程序化判定器读 `terminal_check`，比定额号/费率区间/溯源结构——**不比答案文本**（这是 outcome-based 的关键，和文本相似度划清界限）。
- **L7-B 工具调用**：解析 agent 实际发出的 tool-call，按 `arg_match` 口径比对，纯程序判，可进 CI。
- **L7-C 忠实度**：用 LLM-judge 跑 RAGAS 指标——但 **judge 本身要先在一小批人标样本上校准一致性**，否则是拿不稳的尺子量东西（这点面试被追问"怎么保证 judge 可信"时要主动讲）。
- **pass^k 聚合**：每个 case 跑 k 次，按"k 次全过"折算成 0/1，再算通过率；同时单列**漂移率**（k 次结果不一致的 case 占比）作为小模型稳定性观测。

### 7.4 与既有资产的衔接

- L7-A 的 `difficulty` 维度直接复用 §3 的 EH-01~04 与 `routing_eval` 的 boundary 组——同一批难例，请求层标"路由到哪"，任务层标"做没做成"，靠 `id` 串起来。
- L7-B 的 `query` 可从 `routing_eval` 里 `expect_route=调脚本` 的样本派生，给它补上 `expected_call` 即成。
- L7-C 的 `gold_contexts` 与 `retrieval_eval/gb50016_eval.json`、`match_gold*.jsonl` 同源——检索金标加上"答案事实点"就升级成忠实度评测集。

### 7.5 门线（补入 §5 汇总）

| 指标 | 门线 | 子层 |
|---|---|---|
| 端到端任务成功率（pass^5） | 回归基线（随能力迭代上调） | L7-A |
| 红线违规率（运行态） | = 0 | L7-A（policy） |
| 工具调用 schema 合法率 | ≥ 99% | L7-B |
| 量化后工具调用掉点 | ≤ 2pt（fp16 vs int4） | L7-B |
| RAGAS faithfulness | ≥ 0.9 | L7-C |
| 引用准确率 | = 100%（呼应 C-01 溯源） | L7-C |

> 一句话定位：L1–L4 回答"零件合不合格、能不能验收"，L7 回答"这套 agent 在自己的造价业务上到底做没做成"。前者是 gate，后者是 outcome——正是 AGENT_MS「通用榜单只圈候选、自建 eval 才定胜负」那条的工程落地。

---

## 8. 落地优先级

1. **P0**：以 [`routing_eval/agent_routing_eval.jsonl`](routing_eval/agent_routing_eval.jsonl)（现成 17 条）为种子，按 §3 标注 schema + 分层配额扩样，补 EH-01~04 脏请求，落 `benchmark/retrieval_eval/`。
2. **P0**：L1 路由 + L4 红线两个机器可判层的评测脚本（无需人评，可进 CI 回归）；复用现有 jsonl 的「路由率/红线遵守率」口径。
3. **P1**：L2 门控评测 + τ/w 调参曲线（依赖冻结检索结果）。
4. **P1**：L3 检索评测（版本对齐 + 地域纯净）。
5. **P1**：**L7-C 规范问答忠实度**——`retrieval_eval` 金标加"答案事实点"即可升级，性价比最高、最先能跑出 RAGAS 数。
6. **P2**：**L7-A 端到端组价任务**（终态判定器 + pass^5）+ **L7-B 工具调用**（含量化前后对比）。
7. **P2**：L5 复合拆解、L6 NFR 压测（延迟/隔离/可观测）。
