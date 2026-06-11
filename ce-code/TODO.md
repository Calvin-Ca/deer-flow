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

## Phase B：数据模型改造（🟡 进行中）

> v2 schema 契约 + 构建层富化链 + 解析层按格式重写加固，均已落地。待办按 PRD §3.1 **三轴解析模型**组织：结构轴（建树/profile）+ 粒度轴（chunk/small-to-big）+ 增强轴（表格/谓词）；三轴数据完整后重建索引、新增检索原语。这几块决定三个 agent（问答/算量/审图）的能力天花板。

### 地基 + 富化链（✅ 2026-06-05 ~ 06-08）

- [x] `schema.py`：v2 条款契约（受控词表 + TypedDict）+ `to_v1_compat` 向后兼容桥（重建索引前 engine/metadata 不崩）
- [x] **引用边分型 + 双向**（波1，`extract/references.py`）：`strong`/`weak`/`exclude`/`cross_standard` + `referenced_by` 反向边
- [x] **黑体强条标注**（波1，`extract/strength.py`）：拆 `modal_strength`(语气) / `is_mandatory_clause`(黑体)；官方强条清单优先 → MinerU 字重次之 → 否则保守 False
- [x] **祖先链**（波3，`extract/ancestors.py`）：`ancestor_titles` 章/节标题链
- [x] `extract/build.py` 编排器：v1 条款 → 跑富化链 → v2 + 兼容桥；**无官方清单时保守模式**（语气"应"并回 `is_mandatory`，保证重建索引前召回率不回退）

### 解析层按 MinerU 格式重写 + 加固（✅ 2026-06-08）

> `02_extract_clauses.py` 拆 `read_v1`/`read_v2` 两个 reader，格式差异锁死在 reader 内；表格 HTML 解析作共享工具。

- [x] `detect_format` + `read_v1`/`read_v2`：吐统一规范化元素 schema，`parse_elements` 对 v1/v2 无感
- [x] **表格结构化（解析层）**：v1 `table_body` / v2 `content.html` 同为 HTML 串 → `_HTMLTableParser` + `_expand_spans` 解析成**矩形**二维表（展开 colspan/rowspan 防串列），落 `tables[].body`
- [x] v1 真实坑：`list` 多条款拆分（1.0.1~1.0.7 各自成条款）、`list`/`table` 不再因无 `text` 被丢、page_number 噪声丢弃
- [x] 目录(TOC)剔除：list 级整列(`_is_toc_list`) + 候选级短行，含中/英文目录，避免与正文条款重复
- [x] 交叉引用片段（"8.3节、…"）不误建条款；附录字母条号识别（`E.1`/`E.2.2` 各自成条款、表格精确归位、`_sort_key` 附录排正文后）
- [x] `mineru_api.py` 修输出目录误定位（从本次 ZIP namelist 取，不 rglob 历史产物）；`01` 打印解析耗时
- [x] **实测 GB/T 50500-2024**：561 条款、零重复、35 表全部归具体子条款（附录根 0 表）、1.0.x 齐

### 待办 — 按三轴组织

**① 结构轴**（阶段 1，`02_extract_clauses.py`）：按文档原生目录建树，当前基本可用，待完善：

- [ ] `parse_profile` 配置实际生效：`terminal_stage`（structure|granularity|enrich|index）控制终止阶段；产物路径按 `data/structured/{standard}/{profile}/` 隔离，`04` 索引路径同步隔离
- [ ] `node_type` 枚举覆盖完整（chapter/section/clause/paragraph/table/formula/figure/appendix）；当前部分类型填充不全

**② 粒度轴**（阶段 2，`02_extract_clauses.py`）：切最细自然单元，当前与结构轴合并，待拆分配置：

- [ ] `chunk_granularity` 可配（node | paragraph | natural）与结构树解耦，`parse_profile.chunk_granularity` 实际控制切分粒度
- [ ] `small_to_big` 联动：向量化时拼入 `ancestor_titles` + 所属上级全文；检索返回时回补完整条/节上下文（`retrieval/engine.py` 侧）

**③ 增强轴**（阶段 3，`extract/build.py` 编排）：可选覆盖层，按规范类型条件挂载，仍有三块未完：

- [ ] **数据依赖（阻塞 `is_mandatory_clause` 真值，需服务器侧）**：① GB 50016 官方强条清单 → `data/structured/gb50016_mandatory.json`；② dump `content_list.json` 确认 MinerU 字重字段名 → 改 `strength._BOLD_KEYS`
- [ ] **表格可查询封装**（`extract/tables.py`）：`tables[].body` → `schema.TableRepr`（分表头 + 「给定行列取值」接口，继承所属条款 `is_mandatory_clause`）
- [ ] **适用范围谓词抽取**（`extract/scope.py`）：散文条件 → 结构化谓词；抽不准标 `scope_status: unknown`（当前 build.py 统一填 unknown 进保守召回）
- [ ] **条款级版本/效力**：当前按规范统一填 `status`/`version`/`effective_date`；局部修订细化到条款粒度待实现

**索引重建 + 检索原语**（依赖三轴数据完整）：

- [ ] **重建索引**：`02 → build → 04` 用 v2 数据重建（依赖增强轴数据依赖解除）；`07_eval` 验证召回率不回退
- [ ] 新增 `/filter`（适用范围过滤，依赖 `scope.py` 谓词数据）、`/rerank`（同上）
- [ ] 评测集增加"适用性误判率"指标（依赖 `scope.py` 谓词）

**多规范扩展**：GB 50116（火灾自动报警系统）待收录，GB 50016 未覆盖该专项规范条款。

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
