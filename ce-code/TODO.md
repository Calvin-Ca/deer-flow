# ce-code（组价知识库）· 进度 TODO

> ce-code = **深圳房建组价知识库**。需求见 `PRD.md`、设计见 `DEV.md`、实验记录见 `notebooks/`。
> 任务层进度见 `../ce-services/TODO.md`。
>
> **2026-06-18 重构**：移除规范条文检索 RAG（防火 GB 50016 轨停做），ce-code 收窄为纯组价知识底座；
> 代码重组为 `ingest/`（摄取）+ `cost/`（组价核心）+ `service/cost_api` + `tools/`。防火轨历史靠 git 追溯
> （tag `pre-cost-refactor`），不再在此罗列。

---

## 一、数据底座 —— ✅ 端到端可用

PG `ce_cost`（:5433）已灌齐，组价取数链通（清单→定额→工料机→价）：

| 数据 | 表 | 状态 |
|---|---|---|
| 清单（计量规范） | `bill_spec` | ✅ 2216 条：2024 房建 472 + 安装 1183 + 2013 房建 561（spec 版本隔离共存） |
| 定额（SJG 消耗量） | `quota_item` / `resource` / `quota_resource` | ✅ SJG171 建筑 + SJG170 土方（1257 子目 / 8278 含量 / 991 资源） |
| 信息价（月刊） | `resource_price` | ✅ 2026-05（1138 条，带时效） |
| 费率（2023） | `fee_rate` | ✅ 24 条 |
| 费用构成（50500） | `price_composition` | ✅ 综合单价 6 项 / 工程造价 4 部分 |
| 清单→定额映射 | `bill_quota_map` | ✅ 542 边 / 127 清单（名称匹配 313 边 + 清单套定额补全 `semantic_llm` 229 边，2026-07-06；2024 房建覆盖率 11.2%→26.9%，带 bill_spec_version 版本隔离） |
| 资源↔信息价对齐 | `resource_price_map` | ✅ 43 确定性边（同物异名归一 + 单位换算） |

取数原语（:8100）：`/bill/match`（清单召回，spec 必填）、`/price/compose`（组价，spec 必填）、`/quota`（定额
直取）、**`/price/query`（当期信息价，FR-I01，名称模糊+期号/最新期，动态数据无关 spec）**、
**`/bill/get/{code}`（编码精确查询，FR-C 核对原语，spec 必填）**——均服务器实测通过。
MCP `ce-cost` 四原语：bill_match / quota_lookup / price_compose / **price_query**。

### 2026-07-03 · T9 批次新增原语 + Docker 部署修复（真跑验证 ✅）

- 新增 `/price/query` + `/bill/get` +（MCP）`price_query`（`cost/query.py::query_resource_price/get_bill`）——
  任务层消费方：编排器 price 派发（FR-I）+ `POST /cost/check`（FR-C v1），见 `../ce-services/TODO.md` T9 节。
- Docker 部署修复：`docker/ce-code/docker-compose.yaml` 补 `CE_PG_DSN` 透传（此前容器只传 CE_RERANK_URL，
  容器内 PG 依赖端点全 fe_sendauth 503；真实密码走 `.env` 不入 git）。
- 🔴 **数据任务提级：深圳·2013 定额 / 信息价 / 清单→定额映射入库**——T9-1 后组价**默认口径 = 深圳·2013**
  （PRD C-05），当前 2013 `supports_compose=False`、默认路径 happy path 是诚实 501「仅选码」；同时是
  benchmark **L6-A 组价终态评测**（init_context=深圳2013）的数据前提。管道全套现成（parser→splitter→
  `cost.*` 抽取→`load_pg` 幂等），缺的是 2013 源文档的解析抽取实跑；入库后 `SPEC_REGISTRY` 翻
  `supports_compose` 一个标志即全链可用，任务层零改码。

## 二、国标版本严格隔离 —— ✅ 全栈闭环（2026-06-18）

2013/2024 同 9 位码不同义，混用串库。隔离三层 + 服务器验证零回归：
- **关系库**：`bill_spec` 复合主键 `(code, spec_version)`；`bill_quota_map` 加 `bill_spec_version`。
- **向量库**：`cost_bill_spec_kb`(2024) / `cost_bill_spec_kb_2013` 分库。
- **API/路由**：`config.SPEC_REGISTRY` + `resolve_spec`；`/bill/match`、`/price/compose` spec 必填（未知→400，
  2013 组价数据未就绪→501）。`bill_index --spec` 防建库混版本。
- **任务层**：`ce-services/common/cost_client` 透传 spec（CostAgent 调用前提示用户选版本）。

## 三、/bill/match 召回质量 —— 🟡 瓶颈=召回（详见 notebooks E6–E9 + BACKLOG）

- ✅ 2013 清单源换正确的 **GB-50854-2013（房建计量规范）**，措施码对齐真实项目（解开"同码不同义"死结）。
- ✅ 现浇/预制消歧（cast_type，含 MinerU 空格 bug 修复），零回归。
- ✅ **模板索引文本增强**（E10）：caption 子表类别注入嵌入、门控措施项(0117)，措施桶模板码从"全灭"修到进 top-3。
  **Recall@10 56→60%、Top-3 43→54%、Top-1 持平、零回归**。
- 🔑 真瓶颈仍是召回（miss 多在库的纯检索缺口）；按 PRD §6 Top-1 选码归任务层 LLM，知识层续**提 Recall@10**
  （下一步 sparse 混检 / 本体 underspec）。
- 📌 **任务层选码评测反向定位的召回缺口**（2026-06-23，见 `../ce-services` Step 4 重测）：「C30 现浇混凝土矩形梁」
  金标 `010502011` **未进 top-10**（"钢筋混凝土梁"名实不符），致任务层 LLM 在无正解候选里高置信选错码——
  **这是 sparse 混检（term 精确匹配救名实不符）的直接靶子**。任务层候选内 Top-1 已达 89%（选码不再是瓶颈），
  端到端 Top-1 卡在 80% 主因即此类召回缺口。

---

## 四、造价规范问答检索引擎 —— ⏳ 恢复中（2026-06-22 起）

> **2026-06-22 方向调整**：知识层新增第二能力——**造价规范条文检索**（与组价取数并列）。语料为造价类规范
> （GB 50500/50854/50856、深圳费率/消耗量标准），非防火规范；做法是从 git 历史（`68686329^`）**恢复+适配**
> 被删的 hybrid 检索引擎，而非从零重写。任务层生成侧见 `../ce-services/TODO.md` Norm-QA 章节。

- [x] **A1 恢复引擎 + 接回包结构**（✅ 2026-06-22，import 层）：`git checkout 68686329^` 恢复
  `retrieval/`（bm25/dense/hybrid/rrf/graph/service）+ `ir/{context,feature,query,retrieval}` +
  `index/` + `feature/` + `build.py` + `utils/` + `service/{knowledge_api,retrieve_service}`；
  适配 `ir.{chunk,document,profile}`→`ingest.ir.*`（16处）、build 的 `parser/splitter`→`ingest.*`、
  补恢复漏掉的 `ir/feature.py`。全量 py_compile + 静态 import 解析全绿（运行时第三方依赖/Chunk 接口待服务器验）。
- [x] **A2 验 chunk 兼容**（✅ 2026-06-22，**零适配**）：`ingest.ir.Chunk` 与引擎同期定版（chunk.py 06-15
  定版后只搬家未改），`Chunk.from_dict` 原生读 chunks.json；引擎对 chunk 的属性访问（content/title/
  provenance/node_path/standard_id/parent_id/children_ids/ancestor_*/tables/images/expandable_refs/
  is_grounded）全落现字段集，无 `is_mandatory`（强条 2026-06-12 已降级为 `modal` 表征）。结论：无需改代码。
- [x] **A3 建索引（服务器）**（✅ 2026-06-22）：5 部 GB 计量计价规范（50500/50854 × 2013/2024 + 50856-2024）
  `view→feature→index` 建成（98/31/86/103/158 条），各落独立 collection `building_code_gb_*`，与清单库
  `cost_bill_*` 隔离。embed :8097 + Milvus :19530 复用 cost 轨。**期间修多文档 collection 命名塌缩 bug**
  （commit 995d16b8：build.collection_of 取 store.name 恒为 "default" 全塌；+ collection_name ASCII 化 + resolve_store_dir 嵌套/扁平兼容）。
- [~] **检索质量·表格增强（代码就位，深调留 follow-up）**：计量规则在附录表格、content 只存表标题未进嵌入。
  新增 `feature/tables_text.render_tables`，差异化注入：**dense/context_aug 仅注 caption**（表主题入语义；
  通用表头雷同会同质化向量+被"工程量/计算"无差别命中，故不注）、**bm25 注全表**（项目编码/计量单位/计算规则
  入倒排精确召回）；无表 chunk 零回归。已服务器重建索引验证注入生效（R.2 无表 bm25 分随语料 IDF 变动佐证）。
- [ ] **检索质量·深调（follow-up，需 B3 评测集驱动，勿单查询 whack-a-mole）**：实测"满堂脚手架"——计量表在
  chunk `R.1`（caption="措施项目"非"脚手架"，"脚手架"是 011601001 表体行），全表注入后已进 BM25 语料但未排进
  top-10，疑 rerank（cross-encoder）压低（R.1 caption 对查询语义不亲）。**待 caption-only 改动重建索引后**用
  skip_rerank 隔离 rerank vs 召回，再决定 rerank 策略 / 分词。系统化靠 B3 评测集（见 `../ce-services` B3）。
- [ ] **🔴 检索质量·确证召回缺失（2026-06-23，任务层 Norm-QA 实测反向定位，已隔离根因）**：查"矩形柱按什么
  计量"（gb50854-2024）→ 召回 15 条**无现浇混凝土柱计量规则**，Qwen3 被迫误引装饰柱(Q.6)/钢柱(F.3)/金属
  制品(F.10) 给出错答。隔离链：① **skip_rerank 对照**——rerank on/off 都召回不到 → **排除 rerank、排除 LLM，
  纯召回缺失**（区别于"满堂脚手架"仍疑 rerank 的未隔离态）；② **字段级定位**——2024 全库"矩形柱"仅 1 处
  （2013=3 / SJG=5），且落在 chunk **E.3 的 `tables` 字段**，`content`/`title` 全无 → 检索文本（dense 嵌入 +
  bm25）= content，**表体计量规则从未进可检索文本**，召回必然空手。③ **附带异常**：唯一"矩形柱"还错置在 E.3
  （预制构件）而非现浇柱章节 → **2024 附录 E 现浇柱表体在 MinerU 解析/切块阶段疑似丢行或被插空格**（见 D 类脏
  数据）。**修法两步**：(a) 表体注入检索文本（§四 [~] bm25 全表注入对 E 章柱表实际未生效，待查）；(b) 验 2024
  附录 E 现浇柱表解析质量——若丢行须重解析，若插空格走嵌入期折叠空白（见待办 A）。**这是 sparse 混检 + 表格
  增强的共同靶子，也是 B3 评测前必须先修的"下限拖累"。**
- [x] **A4 检索端点**（✅ 2026-06-22，代码就位待 A3 索引验）：**简化**——不新建 `/norm/search`，复活
  `service/knowledge_api.py` 作 :8100 统一入口（已含 cost_router + /search /expand /clause，是 cost_api 超集）；
  现有通用 `/search` 补 `standard` 别名即服务造价规范。`config.STANDARD_ALIASES` 加 5 部计量计价规范
  （gb50500/50854/50856 × 2013/2024，→ safe_std store 名）。README/DEV 启动改 `python -m service.knowledge_api`。
  collection `building_code_*` 与清单库 `cost_bill_*` 天然隔离。

---

## 待办（按优先级，详见 `notebooks/BACKLOG.md`）

### A. 召回提升（不依赖外部数据，可立即做）
- [x] **模板索引文本增强**（✅ E10，2026-06-18）：caption 子表类别注入嵌入、门控 0117 措施码 → Recall@10 56→60%、
  Top-3 43→54%、零回归。
- [ ] 本体 underspec / **BM25 或 BGE-M3 sparse 混检**（real data Recall 已 justify sparse）；先复用 rank-bm25。
- [ ] chapter/caption 脏数据（MinerU 插空格）嵌入期统一折叠空白。
- [ ] 造价评测集扩充（真实结算 gold），量化 Top-1/Top-3 稳数字。

### B. Phase 2 —— 2013 全功能组价（卡数据）
- [ ] 收 2013 真实项目**实际采用的定额版本 + 价格时点**（口径：不能用 SJG-2024 套 2013 清单，见 BACKLOG）。
- [ ] 建 2013 清单→定额映射；`bill_quota_map` 已备版本维度，数据齐后翻 `SPEC_REGISTRY` 2013 `supports_compose=True`
  即生效（代码不改）。
- [ ] 安装类 03 清单（防雷接地等）待收 GB 50856-2013。

### C. 知识图谱 / 映射富化
- [x] **清单套定额补全首轮收官（2026-07-06，`cost/bill_quota_enrich.py`，commit dd1d9a78）**：LLM 按意思（而非
  名字长得像）给清单项配定额子目，2024 房建覆盖率 **11.2%→26.9%**（127/472 码），净增 74 码 / 229 条边
  （全部 `source='semantic_llm'`，可按 source 整批回滚）。三重防错实测：诚实空 335 码、红线作废 30 条边、
  低置信 0；新配平均每码 3.1 条边（vs 名称匹配 12.9，更全也更收敛）。四靶人眼抽验过：矩形柱/独立基础核心边
  全对；三条过宽边（承台→独立基础等）**定案不修**——对照表定位是候选池，组价时 32B 按特征二次消歧 + 人工闸兜底。
  - [x] **HITL 端到端 demo 数据阻塞已解**（原 🔴，2026-06-28 联调暴露）：`010502006` 现浇矩形柱等本体码已配上
    SJG 本体定额边；剩前端端到端验收（设计说明显式说 2024 口径走全链，见根 `AGENT_TODO.md` §2②）。
  - [ ] 下轮补跑：50856 安装册定向（`--spec-version "GB/T 50856-2024"`，注意定额池现仅 SJG171/170 建筑土方，
    安装项预期大量诚实空）+ 本轮 10 条 LLM 失败码（`--codes` 逗号分隔精确定向，勿整轮重跑——335 条诚实空会被
    浪费重判；失败码从跑批日志 `LLM 失败跳过` 行抓取）。
  - [ ] 剩余 345 未覆盖码继续提覆盖：章节对齐 / 专家标注 / 补定额分册（多为库内无对应定额册，属数据采购线）。
- [ ] `MAPS_TO`（构件→清单）待算量侧接入后建。
- [ ] P1：迁 Neo4j 多跳遍历（清单→定额→工料机）。

### D. 数据精修
- [ ] 定额单位 `$m^3$` LaTeX 残留（`quota.py` 抽取遗留，污染 quota_item/resource 的 unit）→ 清洗后重抽 SJG。
- [ ] SJG `chapter`/`ancestor_titles` 偏弱（无规整目录），入库后抽查。
- [ ] **🔴 `quota_item.chapter` 章节归属抽错（2026-06-28，HITL source_ref 联调暴露）**：`010006-15 矩形柱 木模板`
  的 `chapter="2 实心砖墙"`（矩形柱模板被归到实心砖墙章），致 `/price/compose` 回填的定额 source_ref 误导
  （拼出「… 2 实心砖墙 子目 010006-15」）。属 SJG 定额 ingest 章节归属 bug，回填层如实搬运不猜对错（任务层已确认
  不在编排侧兜数据质量）；重抽 SJG 时修章节归属。
- [ ] **信息价 `resource_price.doc_id` 用通配占位 `SZ-JGXX-PRICE`（2026-06-28，同上联调暴露）**：未落到具体信息价期
  文件号，致命中价 source_ref 来源文件段是占位符（期段 `effective_period` 是真的，可用）。重抽信息价时把 doc_id
  落成实际期文件标识（如 `SZ-JG2605-PRICE`）；**行号级定位**亦在此并办（`resource_price` 无行号列，需加列落库）——
  与任务层 `ce-services` provenance 的「信息价行号待 ingest 补」是同一条。
- [ ] 历史工程库 `hist_bill`（脱敏 + 质量标注，供相似案例对标；[可缓]）。

### E. 规范条文检索（已移除，按需重建）
- [ ] 如需"查计价规则原文"，以干净模块重建条文检索（不复活旧 RAG）；当前 `price_composition` 结构化表已覆盖
  费用构成查询需求。

### F. 取数原语 MCP 化（🟢 代码就位待服务器验，方案见 `DEV.md §7`）
> 方案：`DEV.md §7`「原语对外暴露：HTTP + MCP 双 façade」+ 全局分层 `../ce-services/DEV.md`「组价能力对外暴露」。
> 目的：三原语成为横切共享底座（算量/审图/FM 直接以 tool 调），服务中间步请求 + 将来强模型自由编排。
- [x] **确认 deer-flow MCP 注册方式**（✅ 2026-06-27，核实 `backend/.../deerflow/mcp`）：配 `extensions_config.json`
  `mcpServers`（同文件已注册 skills）；`MultiServerMCPClient`(`langchain-mcp-adapters`) 启动加载缓存；transport
  选 `http`(FastMCP streamable-HTTP)；`tool_name_prefix=True` → 工具名带 `{server_name}_` 前缀。**纠正**：`type:http`
  是 MCP 协议非任意 REST，现有 :8100 REST 不能直接当 MCP server。详见 `DEV.md §7`。
- [x] 用 **FastMCP（streamable-HTTP）** 起 MCP server（✅ 代码就位，`service/mcp_server.py`）：把 `bill_match` /
  `quota_lookup` / `price_compose` 包成 `@mcp.tool`（**复用 `search_bill`/`get_quota`/`compose_price`，不动取数内核，
  不反代 REST**）；server 名 `ce-cost`（工具名 → `ce-cost_*`）；`stateless_http=True` 只读无 session；随
  `service.knowledge_api` 挂 :8100 `/mcp`（`app.mount("/", streamable_http_app())` + 父 lifespan 跑 session_manager，
  Starlette 不自动跑挂载子应用 lifespan）。`extensions_config.json` `mcpServers` 已加 `ce-cost`（type:http）。
  HTTP REST `service.cost_api` 保留供任务层编排（零改动）。`pyproject.toml` 加 `mcp>=1.27`。
- [x] **红线复述进 MCP schema**（✅ 原语自带护栏，不依赖上层编排）：`spec` 必填无默认（`resolve_spec`，缺省/未知→
  `ToolError`）/ `price_compose` 缺价 `no_source` 不杜撰（取数内核保证）/ 2013 `supports_compose=False`→`price_compose`
  与 `quota_lookup` 结构化拒答（`_require_compose_ready`）/ `bill_match` 只给候选不定 Top-1（docstring 明示）。
- [ ] **服务器验证**：① `cd ce-code && uv sync`（装 `mcp`）；② 重起 :8100（`.venv/bin/python -m service.knowledge_api`），
  `curl :8100/health` 仍 OK；③ 重起 deer-flow gateway，确认 agent 加载到 `ce-cost_bill_match` / `ce-cost_quota_lookup`
  / `ce-cost_price_compose` 三工具；④ 实调一次 `bill_match`（spec=2024）+ `price_compose`（缺价资源应见 `no_source`）。

---

## G. 部署 / Docker（2026-07-02）

- [x] **精排 GPU 拆分（✅ 2026-07-02，本地 py_compile + compose config 过）**：知识层 :8100 唯一的本地 GPU
  消费者=cross-encoder 精排（`hybrid_retriever` 进程内 `FlagReranker`）。拆成独立服务 `service/rerank_api.py`
  （:8098，`POST /rerank`）；`hybrid_retriever.rerank` 改 HTTP 调 `config.RERANK_URL`（同 embedding :8097 做法），
  服务不可达→静默 fallback RRF（连不上缓存一次判定、不逐查询空等超时）。`config.py` 加 `RERANK_URL`/`RERANK_TIMEOUT`。
  Docker：新增 `docker/ce-rerank/{Dockerfile,docker-compose.yaml,.env.example}`（GPU）；`docker/ce-code/docker-compose.yaml`
  的 knowledge 去 GPU reservation（现纯 CPU）。**待服务器**：起 :8098 + 重起 :8100，验 `/bill/match` 召回精排质量不回归。
- [ ] **🔴 修 `docker/ce-code/Dockerfile`（重构前旧版，build 不出当前代码）**：`COPY ce-code/core/`（该目录已不存在）、
  `CMD python -m retrieval.server`（入口早改 `service.knowledge_api`）、且缺 `psycopg[binary]`（cost 取数）/`mcp`
  （:8100/mcp）。需重写 COPY 目录 + 依赖 + CMD；因精排已拆出，**基底可从 `pytorch-cuda` 换 `python:3.12-slim`**（纯 CPU、镜像大幅瘦身）。
  （ce-services Dockerfile 的同类陈旧问题已于 2026-07-02 修，可参照。）
