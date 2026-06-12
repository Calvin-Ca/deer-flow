# ce-code（知识层）· 进度 TODO

> 知识层（数据 + 检索）的执行进度与重构历程。需求/设计见同目录 `PRD.md`；任务层进度见 `../ce-services/TODO.md`。

---

## 阶段 0：技术 POC（✅ 已完成）

MinerU 解析 + 条款树提取 + 质量审核，在 GB 50378-2006 和 GB 50016 上验证通过。

## 阶段 1：检索 MVP（✅ 已完成）

- [x] GB 50016 PDF → MinerU 解析（`split_and_parse.py` 分块 80 页/块，`hybrid-auto-engine` 后端，`CUDA_VISIBLE_DEVICES=2`）
- [x] 安装 retrieval 依赖（pymilvus、rank-bm25、requests；`uv add` 写入 `pyproject.toml`）
- [x] `04_build_index.py`：建 BM25 + Milvus 向量双索引（GB 50016 已建，**911 条条款**）
- [x] `05_retrieve.py`：混合检索 + 引用扩展（BM25 + 向量 + RRF 合并 + 引用图扩展 + Rerank）
- [x] 评测集 `data/eval_set/gb50016_eval.json`（45 条用例）；`07_eval.py` 评测已跑
- [x] 向量索引建立后补充 `flush` 确保数据落盘

### 评测集

> 评测契约（用例格式 + 核心指标：召回率/引用召回率/适用性误判率/造价侧命中率）见 `PRD.md §四 验收标准`。当前已建 GB 50016 评测集（45 条，见上阶段 1 勾选）。

---

## 重构历程（行为保持，不改 schema、不重建索引）

### Phase A：检索引擎收敛（✅ 2026-06-01）

把检索逻辑从 importlib 反向加载的 POC 脚本收敛进 `retrieval/` 包。

- [x] 建 `retrieval/`（`config.py` + `engine.py`，从 `05_retrieve.py` 搬）→ 05 改薄（保留 `retrieve` 名兼容）
- [x] 知识服务 `server.py` 接 retrieval + 拆原语端点 `/search` `/expand` `/clause`
- [x] **服务器验证**：`07_eval.py` 召回率与重构前一致（行为保持核心证据）

**痛点（重构前，已消除）**：06/10 用 `importlib` 按 `05_retrieve.py` 文件名反向加载 → 改名即崩；store-dir 解析 / collection 命名 / DEFAULTS 在多处重写。

### v3：知识层瘦身（✅ 2026-06-03）

任务层迁出后，知识服务只留检索原语。

- [x] `service/server.py` 删 `/qa` `/retrieve` 端点 + generation 依赖，只留 `/search` `/expand` `/clause` `/health`
- [x] `service/` 现仅剩 `server.py`（generation/orchestration/params/queries 已迁至 `ce-services/`）
- [x] 退役删除 POC CLI `06/08/09/10`；保留 `05_retrieve.py` / `07_eval.py`（只依赖 retrieval）
- [x] **行为等价**：`/search` 内部 `bm25_top_k = vector_top_k = top_k*2` 调 `retrieval.engine.search`，与重构前 orchestration 直调参数逐字一致 → `07_eval.py` 召回率不变

### Docker（✅ 2026-06-04）

- [x] `docker/ce-code/`：知识服务镜像（pytorch 基底，含 GPU/FlagEmbedding，~6GB）+ compose（仅 :8100）
- [x] `network_mode: host` 直连宿主机 Milvus/vLLM

> 部署注记：后台起服务**勿用 `nohup`**（stone 服务器 `Exit 125` 静默失败），改用 `setsid` 或 tmux；诊断"起不来"先前台直跑看真实报错。

---

## Phase B：数据模型改造（🟡 进行中 · 2026-06-12 设计转向）

> **设计转向（2026-06-12）**：废弃"强条召回"铁律与三轴顺序模型，改为 PRD §3.1 新模型 **节点树（唯一真值）+ 多表征（语义投影）+ 粒度视图（索引期选层）**。强条/法律强制整套机制移除；语气降级为 `modal` 表征（可选召回通道，不全局置顶）。下方"地基+富化链/解析层重写"为转向前已完成项，保留作历史；**实际待办以「按新模型组织」段为准**（旧"按三轴组织"段已被取代）。

### 地基 + 富化链（✅ 2026-06-05 ~ 06-08）

- [x] `schema.py`：v2 条款契约（受控词表 + TypedDict）+ `to_v1_compat` 向后兼容桥（重建索引前 engine/metadata 不崩）
- [x] **引用边分型 + 双向**（波1，`extract/references.py`）：`strong`/`weak`/`exclude`/`cross_standard` + `referenced_by` 反向边
- [x] **黑体强条标注**（波1，`extract/strength.py`）：拆 `modal_strength`(语气) / `is_mandatory_clause`(黑体)；官方强条清单优先 → MinerU 字重次之 → 否则保守 False
- [x] **祖先链**（波3，`extract/ancestors.py`）：`ancestor_titles` 章/节标题链
- [x] `extract/build.py` 编排器：v1 条款 → 跑富化链 → v2 + 兼容桥；**无官方清单时保守模式**（语气"应"并回 `is_mandatory`，保证重建索引前召回率不回退）

### 解析层按 MinerU 格式重写 + 加固（✅ 2026-06-08）

> `02_parse_hierarchy.py`（原 `02_extract_clauses.py`，重命名为层级化解析流水线）拆 `read_v1`/`read_v2` 两个 reader，格式差异锁死在 reader 内；表格 HTML 解析作共享工具。

- [x] `detect_format` + `read_v1`/`read_v2`：吐统一规范化元素 schema，`parse_elements` 对 v1/v2 无感
- [x] **表格结构化（解析层）**：v1 `table_body` / v2 `content.html` 同为 HTML 串 → `_HTMLTableParser` + `_expand_spans` 解析成**矩形**二维表（展开 colspan/rowspan 防串列），落 `tables[].body`
- [x] v1 真实坑：`list` 多条款拆分（1.0.1~1.0.7 各自成条款）、`list`/`table` 不再因无 `text` 被丢、page_number 噪声丢弃
- [x] 目录(TOC)剔除：list 级整列(`_is_toc_list`) + 候选级短行，含中/英文目录，避免与正文条款重复
- [x] 交叉引用片段（"8.3节、…"）不误建条款；附录字母条号识别（`E.1`/`E.2.2` 各自成条款、表格精确归位、`_sort_key` 附录排正文后）
- [x] `mineru_api.py` 修输出目录误定位（从本次 ZIP namelist 取，不 rglob 历史产物）；`01` 打印解析耗时
- [x] **实测 GB/T 50500-2024**：561 条款、零重复、35 表全部归具体子条款（附录根 0 表）、1.0.x 齐

### 待办 — 按新模型组织（节点树 / 多表征 / 粒度视图）

> 新流水线（见 PRD §3.2）：阶段 1 结构层（建节点树 `nodes.json`）→ 阶段 2 表征层（挂 `reprs`）→ 阶段 3 索引（按 `index_granularity` 选粒度视图）。代码任务编号 T1–T10，依赖关系见下。**一次只动一个变量，每步过 `07_eval` 护栏。**

**波1 — 拆强条 + 立节点树骨架**（无新依赖，纯重构，可立即开工）：

- [x] **T1 `schema.py` 换契约**（✅ 2026-06-12）：`Clause` → `Node` + `Representation` + `Provenance`；新增 `parent_id`/`children_ids`/`reprs`、结构层审计 `path_source`/`path_confidence`、溯源 `provenance`（`block_idx` 回指 MinerU 原始块，原始留 `data/parsed/` 不可变）、`ancestor_paths`；`level` 由号段数推导不取 text_level；工厂 `new_node()`/`empty_condition()`。删 `is_mandatory_clause`/`_HARD_MODAL`/`to_v1_compat`/`empty_scope`/`ApplicableScope`/`TableRepr`（v1 兼容桥退役）。保留 `RefType`/`EXPANDABLE_REF_TYPES`/`Reference`/`Modal`（语气词表，注释钉死「无法律含义」）。冒烟测试通过；下游 `extract/build.py` 待 T5 改编排。
- [x] **T2 结构层产树**（✅ 2026-06-12，commit be0ab594）：`02_parse_hierarchy.py` 的 `GranularityAxis` 保留 parent/child 产 `nodes.json`（单一真值，含 `parent_id`/`children_ids` + `provenance` 回指 MinerU 块 + `path_source`/`path_confidence` 审计），引用图分型 + 祖先链作"固有事实"在结构层一次算定；目录解耦为打标（`is_toc` 标签保留不并入正文），条款号识别解耦为无状态 `classify_heading`；删 `build.enrich`/`--official` 强条分支。
  - **T2 结构层职责重划**（✅ 2026-06-12）：原结构轴越权——把 `clause_path`/`node_type`/`level`/祖先链/`path_source` 等强行打在每个块上，而这些非"打目录标签"层能精准做好。改为：`StructuralAxis` 瘦身为**纯目录打标器**，每块只留可靠标签 `is_heading`/`text_level`/`is_toc`（+ `standard_id`/溯源），删标题栈与 `_process_heading`/`_annotate_content`；**条文号识别 + `node_type` + 建树 + 祖先链 + 引用图全部下沉进 `GranularityAxis`**（`classify_heading` 改自包含纯函数，`node_type` 由号段数自推）。`nodes.json` 形态不变，`03`/`04` 不受影响；`structure.json`（仅 02 自用）改为扁平目录标签块。⚠️ 待服务器重跑 02 对齐基线。
  - **T2 结构轴独立成文件 + catalog 升级值标签 + 目录定位（方案5）**（✅ 2026-06-13）：`StructuralAxis` 从 `02` 抽出为 `pipeline/structural_axis.py`（与建节点树解耦、可单测）。`is_catalog`(bool) 升级为 `catalog`(值)——块本身是目录页→`"目录"`，否则→**所属目录条目标题**（属于目录里哪一条），目录前/无目录→`None`。定位用**方案5（混合）**：目录页解析成有序条目表（骨架真值，兼容括号页码与点导引裸页码）→ 正文按文档序单调前瞻扫描、归一化匹配条目切换"当前条目"，条(x.x.x)不在目录则归属其节；无目录退化为以 `is_heading` 标题作边界。目录页识别改**区域判据**（连续成行 ≥`MIN_TOC_RUN` / 整列），孤立"行尾带数字"正文短行不误判（守"不做减法"）。建树层 `02` 跳过判据 `is_catalog` → `catalog=="目录"`。⚠️ 阈值/尾页码正则按常见排版设默认，待服务器跑真规范微调；建树层尚未改用 `catalog` 建树（仍走 `classify_heading` 号段路径）。另加 `catalog_source` 审计字段（`toc_page`/`toc_match`/`inherited`/`heading_fallback`/`none`），让方案5各定位来源在结果可见、`print_stats` 按来源分解计数。
  - **T2 删冗余透传 + is_heading 改用 text_level**（✅ 2026-06-13）：`FormatAdapter` 删 `raw`（整条 MinerU dict 冗余，需原件靠 `block_idx` 回查 `data/parsed/`；唯一读者 `strength._bold_from_raw` 本就拿不到、且强条已废弃）、`list_items` 展开后不再逐块保留（`_flatten` 里 `pop`）。`is_heading`(派生 bool) 改为 `text_level`(MinerU 标题层级原样透传，仅标题块有键)，消费方 `02` 建树判定 + 结构轴 `_locate`/`print_stats` 同步改 `text_level is not None`。审计「操作+改键」仅剩 `page_idx→page`(+1)、`table_caption→text`、`table_body→body` 三处实质转换（合理改名，与 is_heading「改名却丢原值」不同）。
- [x] **T3 删强条排序**（✅ 2026-06-12）：`retrieval/engine.py` 去掉 `rerank()`/`search()` 里 `mandatory + non_mandatory[...]` 的强条置顶与 `vector_search` 的 `filter_mandatory`；结果纯按 RRF/rerank 排序后切 `top_k`。残留 `MILVUS_OUTPUT_FIELDS` 的 `is_mandatory` 与 stats 观测留 T4/T6 清理。
- [ ] **T4 索引去强条字段**（`04_build_index.py`）：Milvus schema 删 `is_mandatory` 字段 + 其 INVERTED 索引；`metadata.json` 同步去字段；加 `node_id`/`parent_id`/`granularity` 判别字段。索引路径改 `data/vector_store/{standard}/{profile}/`。
- [ ] **T5 改编排**（`extract/build.py`）：删保守模式、官方强条清单、`_diff_mandatory`、`to_v1_compat` 调用；改为"跑固有事实 + 表征注册表 → `nodes.json`"。
- [ ] **T6 服务层清理**（`service/server.py`）：删 `mandatory_clauses_count`、`★强条` 日志；`/search` 返回挂 small-to-big 父节点上下文。

**波2 — 粒度视图 + 表征注册表**（依赖波1）：

- [ ] **T7 粒度视图**：新增 `view(tree, index_granularity) → 检索单元`（索引期函数）；`ParseProfile` 去掉旧 `chunk_granularity`/`enrichment`，加 `index_granularity`（section|clause|paragraph）+ `reprs` 列表 + `small_to_big`。
- [ ] **T8 表征注册表**（`extract/` → `reprs/`）：免费表征落地——`raw`/`sparse`/`context_aug`（接管 `ancestors.py`）/`table_struct`（接管现表格 HTML 解析）/`modal`（复用 `strength.parse_modal_strength` 正则，删 `is_mandatory` 法律逻辑，产出 `reprs.modal`）；`condition` 谓词（`reprs/condition.py`，抽不准标 `scope_status:unknown`）。
- [ ] **T9 small-to-big 检索**（`retrieval/engine.py`）：细粒度命中后靠 `parent_id` 上探返回整条/整节；`modal` 作可选 filter 通道（query 带强制意图时启用）。

**波3 — LLM 表征 + 评测改造**（依赖波2 + Qwen3）：

- [ ] **T10 评测换指标**（`07_eval.py`）：删"强条召回率"首要指标，改 Recall@k / 引用召回 / MRR / 金标秩；按**包含关系**判命中（配合 small-to-big）。`03_review_quality.py` 同步：删强条统计/误标检测，改节点树健康（孤儿节点 / 空内容 / 表格归属 / 悬空引用）。
- [ ] **LLM 表征**（`reprs/summary.py`、`reprs/questions.py`）：调 Qwen3 生成摘要 / 假设问题表征，入 `dense` 多通道。
- [ ] **重建索引 + 验证**：`02 → build → 04` 用新模型重建 GB 50016；`07_eval` 对比新旧召回（注意基线口径已换）。

**多规范扩展**：GB 50116（火灾自动报警系统）待收录。

---

## Phase C：造价知识底座（CostAgent / 算量组价 agent）（⬜ 待办）

> 对应 PRD §3.3、`cost_agent_prd.md` 八 / `cost_agent_tech.md` 三、六，以及 CostAgent M0 数据底座里程碑。新增**关系库 + 知识图谱**两层与造价检索原语；与 Phase B（防火轨数据模型）解耦，可并行。
> 范围：**单地区房建**先行（与 CostAgent MVP 一致）；算量引擎/图纸解析/编排在任务层，不在此。

### 数据资产（关系库优先）

- [ ] 关系库 PostgreSQL 建表：`bill_spec` / `quota_item` / `quota_resource` / `resource` / `resource_price` / `hist_bill`，强制带 `version` + `region`，价格带 `effective_period`
- [ ] GB 50500 + GB 50854 清单计量规范结构化入 `bill_spec`（复用 MinerU 解析 + 规则，含 calc_rule + feature_schema）
  - [x] **GB/T 50500-2024 已过 02 解析**（561 条款、35 表结构化、附录条款归位），下一步从条款树抽 `bill_spec` 字段入库
  - [ ] GB/T 50854-2024 待重新解析（前次 01 运行命中 `mineru_api` 输出目录误定位 bug，已修；需重跑确认产物正确）
- [ ] 单地区定额库导入 `quota_item` + `quota_resource` + `resource`（定额电子表清洗）
- [ ] 价格库导入 `resource_price`（信息价/市场价，带 `effective_period` 时效）
- [ ] 历史工程库 `hist_bill`（脱敏 + 质量标注，供审核轨对标）

### 知识图谱

- [ ] **P0**：用 PG 关联表模拟「构件→清单→定额→工料机」关系（`MAPS_TO` / `APPLIES` / `CONSUMES`），跑通组价取数
- [ ] **P1**：迁 Neo4j，多跳遍历（清单→定额→工料机）

### 向量库 + 检索原语

- [ ] 造价 `bill_spec_kb` collection（BGE-M3 dense+sparse 混检），供清单匹配候选生成
- [ ] 新增 `/price/compose`（清单项+region→工料机含量+价格：KG + 价格库；**先跑通取数路径**）
- [ ] 新增 `/bill/match`（构件→清单候选：BGE-M3 混合召回 + KG 约束 + LLM 决策；依赖上一步 KG 跑通）
- [ ] 新增 `/quota/{region}/{code}`（定额子目直取）

### 造价评测集

- [ ] 清单编码匹配：`match_gold.jsonl`（构件→编码标注），指标 Top-1 ≥ 85% / Top-3 ≥ 95%
- [ ] 定额套用：对照已结算项目，定额套用准确率 ≥ 85%
- [ ] 红线门禁：未达准确率红线的原语默认「只建议不定稿」（HITL 在任务层兜底）

**模型/部署待评估项**：造价轨 embedding 用 BGE-M3 vs 复用规范轨 bge-large-zh-v1.5 是否统一为单服务（见 PRD §3.3 造价数据资产 + DEV.md 造价轨实现）。
