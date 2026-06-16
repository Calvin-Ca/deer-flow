# ce-code（知识层）· 进度 TODO

> 知识层 = **算量组价的造价知识底座**（数据 + 检索）。需求/设计见同目录 `PRD.md`、`DEV.md`；任务层进度见 `../ce-services/TODO.md`。
>
> **阅读视角**：早期阶段（GB 50378 / GB 50016 防火规范）是**检索引擎的 POC 验证**——目录结构清晰、便于打磨建树/引用图/召回。引擎验证后应用于**算量知识**（清单 GB 50500 / 计量 GB/T 50854，同为带目录的规范 PDF，走同一流水线）；**组价知识**（定额/价格/费用/历史 → 关系库 + KG）是 Phase C 主线。下方"防火轨"重构历程按此视角理解为引擎打磨记录。

---

## 阶段 0：技术 POC（✅ 已完成）

MinerU 解析 + 条款树提取 + 质量审核，在 GB 50378-2006 和 GB 50016 上验证通过。

## 阶段 1：检索 MVP（✅ 已完成）

- [x] GB 50016 PDF → MinerU 解析（`01_split_and_parse.py` 分块 80 页/块，`hybrid-auto-engine` 后端，`CUDA_VISIBLE_DEVICES=2`）
- [x] 安装 retrieval 依赖（pymilvus、rank-bm25、requests；`uv add` 写入 `pyproject.toml`）
- [x] `04_build_index.py`：建 BM25 + Milvus 向量双索引（GB 50016 已建，**911 条条款**）
- [x] `05_retrieve.py`：混合检索 + 引用扩展（BM25 + 向量 + RRF 合并 + 引用图扩展 + Rerank）
- [x] 评测集 `data/eval_set/gb50016_eval.json`（45 条用例）；`07_eval.py` 评测已跑
- [x] 向量索引建立后补充 `flush` 确保数据落盘

### 评测集

> 评测契约（用例格式 + 核心指标：Recall@k/引用召回率/**地区隔离准确率**/清单候选集命中率/定额套用准确率）见 `PRD.md §6 验收标准`。当前已建 GB 50016 评测集（45 条，见上阶段 1 勾选）。
> ⚠️ 旧"适用性误判率"已随 PRD v3 改为"地区隔离准确率"（时效性入库即校验后，废止/过渡期不再是检索期问题，只剩地区串库需评测）。

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

### 四层重构 + 去数字化（✅ 2026-06-13，纯结构移动·零逻辑改动）

把"数字前缀 script（`01_/02_/04_`）不可 import → 各处 sys.path hack + 裸 `import catalog_labeler`"的旧味清掉，按**四层 + 编排同级**收敛（用户拍板方案）：

- **新布局**：`core/`（贯穿契约 schema/parse_profile/view）、`parser/`（解析层：mineru_client/pdf_parser/split_parse/format_adapter，去数字）、`splitter/`（切分层：base/toc/catalog_labeler/tree_builder + 并入的 references；`splitters`→`splitter`）、`reprs/`（表征层）、`retrieval/`（检索/服务层：engine/config/server + 新 `indexer.py`）、`tools/`（评测/审核 03/05/07 去数字 + 运维 .sh）；编排 `parse.py`（阶段0）/ `build.py`（阶段1→3 单入口，按 `terminal_stage` 跑）放根同级。
- **去 sys.path hack**：`packages=[]`（不装包，从 ce-code 根运行）；库层全绝对 import（`from core import schema` / `import splitter`），编排 `python build.py` / 服务工具 `python -m retrieval.server` / `python -m tools.eval`。所有 `sys.path.insert` 已删。
- **包撤销/合并**：`extract/` 撤销（references 并入 `splitter/`）、`service/` 撤销（server 并入 `retrieval/`）、`pipeline/` 撤销（01→parser、02+04 合入 build.py、03→tools）。`02/04` 的 CLI 合并为 build.py 单入口（结构层 + 索引层按 terminal_stage 串起）；04 的索引库函数抽成 `retrieval/indexer.py`。
- **统一目录命名**：build.py 用单一 `_safe()` 给 structured / vector_store 目录命名，消除旧 02/04 两套 sanitize 分歧（data/ 不入 git，无既存产物，安全）。
- **验证**：全模块 `py_compile` OK；从根 import 全层解析 OK（无 hack）；E2E 合成（FormatAdapter→splitter(toc)→reprs.enrich→view→indexer.node_to_row）阶层/引用/表征/行生成全对；`build.py`/`parse.py --help` 与 `retrieval.server` app import OK。⚠️ Milvus/embedding 路径仍待服务器（同 🏁 里程碑跑）。

### tools 去重 + parse.py 下沉（✅ 2026-06-14）

- **tools 去重 + 清 v1 强条**：`retrieve_cli.py` 删 `run_eval`（与 `tools/eval.py` 重复）回归纯单查询调试；`eval.py` 删强条口径（`must_be_mandatory`/`mandatory_recall`，通过判定统一为期望召回率 ≥ 0.5），强条机制 2026-06-12 已废（见 schema.py）。
- **parse.py → parser/__main__.py**：阶段 0 编排只调 `parser.*` 子命令（包内编排，区别于 build.py 跨包编排留根），下沉为包级入口 `python -m parser single / split`；同步 README/DEV 引用。
- **删孤儿目录**：`extract/`、`pipeline/` 仅余过期 `__pycache__`（源码早已并入 splitter/parser/build），git 未跟踪，清掉。

### 分层重构：统一 IR + 各层基类/factory/多策略（✅ 2026-06-15）

把各阶段数据统一为**显式 IR**（`ir/` 全 `@dataclass` + `to_dict/from_dict`），各层做成「基类 + factory + 可插拔多策略」，当前实装一条链路、其余占位（抛 `NotImplementedError`）：

- **IR（ir/）**：`document`(Document/Block) / `chunk`(Chunk/Reference/Provenance) / `feature`(ChunkFeature) / `query`(RetrievalQuery) / `retrieval`(RetrievedChunk) / `context`(KnowledgeContext) / `profile`(ParseProfile)。`Chunk` 替代旧 `schema.Node`，**保留 `node_path` 作文档内结构地址 / HTTP 契约键**，**新增 `chunk_id`**（= `standard_id#node_path`，全局唯一定位键，缺省由 `__post_init__` 派生、落盘；旧 `node_id` 全层废除）。
- **解析层 parser/**：`base`+`factory` + ★`mineru`(包 format_adapter，产 Document) + ◌`unstructured`。
- **切分层 splitter/**：`base`(`split(Document)->SplitResult`)+`factory` + ★`toc_splitter`(承旧 toc) + ◌`semantic`/`tree`。（本批 toc 内部仍 `catalog_labeler`/`tree_builder`/`references` 三件分文件 + dict 管道、出口转 Chunk；三件 2026-06-15 后续合并入单一 `toc_splitter.py`，见下条。）
- **表征层 feature/**（承旧 `reprs/`）：`base`+`pipeline` + ★`raw`/`bm25`(=旧 sparse)/`dense`/`context_aug` + ◌`keyword`/`graph`。
- **索引层 index/**（拆旧 `indexer.py`+`view.py`）：★`manager`(view 选粒度 + **空骨架过滤** + 行准备 + 编排) / `bm25_index` / `vector_index` / `metadata_index` + ◌`graph_index`。
- **检索层 retrieval/**（拆旧 `engine.py`）：`base` + ★`dense_retriever`/`bm25_retriever`/`hybrid_retriever`(RRF+引用扩展+rerank，逐字保持旧召回)/`rrf`/`service`(RetrievalService) + ◌`graph_retriever`。
- **服务层 service/**（承旧 `server.py`+`build.py`）：★`build_service`(阶段1→3 编排) / `retrieve_service`(可观测性) / `knowledge_api`(:8100，**4 端点契约逐字不变**)。
- **utils/**：`tokenizer`(字符级分词，建/检索共用) / `text_cleaner` / `logger`；**根 `config.py`**：服务地址/别名/collection 命名（承旧 `retrieval/config.py`）。
- **删除**：旧 `core/{schema,parse_profile,view}.py`、`reprs/`、`retrieval/{engine,indexer,config,server}.py`。产物 `nodes.json`→`chunks.json`。
- **验证**：全 65 文件 `py_compile` 通过；各层从根 import 无 sys.path hack；`tests/test_splitter_pure.py` 14/14 + 新 `tests/test_ir_pipeline.py` 7/7（IR 往返 / 合成 Document→Chunk→feature→view 全链路 / 空骨架过滤 / RetrievedChunk 契约 / RRF·引用扩展）。⚠️ Milvus/embedding 真链路仍待服务器（同 🏁 里程碑）。

### parser/splitter 类型化 + splitter 收口（✅ 2026-06-15）

承上条分层重构，进一步消改名缝、收口 splitter 内部件、放开切分深度：

- **无类型 node dict → 类型化**：内部不再有平行字段词汇表（`node_type` vs `chunk_type`），统一 `chunk_type`；出口无改名映射，魔法键 `_catalog`/`_skeleton` 改为有类型的声明字段；字段拼错从「静默 `.get` 返回 None」变成「dataclass 属性错误立即报」。
- **splitter 三内部件合并**：`catalog_labeler.py` / `tree_builder.py` / `references.py` 合并进单一 `splitter/toc_splitter.py`（按 §1 引用 / §2 目录打标 / §3 建树 / §4 切分策略 分段）——外部无消费方、随 TOC 法内聚，纯函数仍可独立单测（`tests/test_splitter_pure.py` 从 `toc_splitter` 导入）。
- **切分深度可控**：`Splitter.split` 接 `max_depth`+`subsplit` 切分期参数（profile 透传 `toc_max_depth`/`subsplit`），决定树建到哪一层 / 目录层下是否按编号细分；与索引期 `index_granularity` 视图选层正交（替代旧未实装 `clause_strategy`/ClauseSplitter 过渡设计）。
- **registry 驱动启动**：`parser/__main__.py` 与 `splitter/__main__.py` 不再硬编码工具名，遍历 factory `REGISTRY` 把声明了 `run_cli` 的工具/切法挂成 `python -m parser <工具>` / `python -m splitter <切法>`（占位工具/切法无 `run_cli` → 不出现在 CLI）。
- **同步**：`tests/test_ir_pipeline.py` 改掉删除的 `split(..., profile=)` 调法（改 `max_depth`/`subsplit`）；README 目录树去三件幽灵文件、DEV §2.2 字段名/切分深度描述对齐。

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

> 新流水线（见 PRD §3.2）：阶段 1 结构层（建节点树 `chunks.json`）→ 阶段 2 表征层（`feature/` 挂表征）→ 阶段 3 索引（按 `index_granularity` 选粒度视图）。代码任务编号 T1–T10，依赖关系见下。**一次只动一个变量，每步过 `tools.eval` 护栏。**

> **⚠️ 下方「核心现状（2026-06-13 评估）」块为历史快照（旧名 `nodes.json`/`02`/`04`/`engine`/`server`/`07_eval`），其描述的脱钩问题在 2026-06-15 分层重构后路径已全部更名（见上「分层重构」条目）；保留存真，开放待办以新模块名为准。**

> **⚠️ 核心现状（2026-06-13 评估）：流水线后半段已与 `nodes.json` 脱钩，护栏事实上失效。**
> - **生产者已迁移**：`02` 现在只产 `nodes.json` + `structure.json`，**不再产 `*_clauses.json`**。
> - **消费者全在 v1**：`extract/build.py`（读 `*_clauses.json`→`*_clauses_v2.json`）、`04`（读 `*_clauses.json`，建 `is_mandatory` INVERTED 索引）、`engine`、`server` 全部仍吃 v1 dict 行格式。
> - **后果**：`nodes.json` 当前**零下游消费者**，`02 → build → 04` 这条链是断的（格式对不上）。`07_eval` 还能跑，只因它打的是 **T2 之前旧 `*_clauses.json` 建的陈旧索引**——新树从未被索引/检索验证过。"每步过护栏"的纪律已无所附丽。
> - **据此重排（见下）**：把目标从"删 is_mandatory 字段"（低价值清理）重定为**"让 `nodes.json` 端到端可建索引、可被 `07_eval` 与 v1 基线对比"**；纯删除型任务（T4/T6 去字段）顺手捎上。原"波1/波2/波3"按概念分组（拆强条 / 粒度 / LLM）改为按**执行批次**分组（接通链路优先）。**第 1 步打通前，护栏不算活。**

**波1 — 拆强条 + 立节点树骨架**（无新依赖，纯重构，可立即开工）：

- [x] **T1 `schema.py` 换契约**（✅ 2026-06-12）：`Clause` → `Node` + `Representation` + `Provenance`；新增 `parent_id`/`children_ids`/`reprs`、结构层审计 `path_source`/`path_confidence`、溯源 `provenance`（`block_idx` 回指 MinerU 原始块，原始留 `data/parsed/` 不可变）、`ancestor_paths`；`level` 由号段数推导不取 text_level；工厂 `new_node()`/`empty_condition()`。删 `is_mandatory_clause`/`_HARD_MODAL`/`to_v1_compat`/`empty_scope`/`ApplicableScope`/`TableRepr`（v1 兼容桥退役）。保留 `RefType`/`EXPANDABLE_REF_TYPES`/`Reference`/`Modal`（语气词表，注释钉死「无法律含义」）。冒烟测试通过；下游 `extract/build.py` 待 T5 改编排。
- [x] **T2 结构层产树**（✅ 2026-06-12，commit be0ab594）：`02_parse_hierarchy.py` 的 `GranularityAxis` 保留 parent/child 产 `nodes.json`（单一真值，含 `parent_id`/`children_ids` + `provenance` 回指 MinerU 块 + `path_source`/`path_confidence` 审计），引用图分型 + 祖先链作"固有事实"在结构层一次算定；目录解耦为打标（`is_toc` 标签保留不并入正文），条款号识别解耦为无状态 `classify_heading`；删 `build.enrich`/`--official` 强条分支。
  - **T2 结构层职责重划**（✅ 2026-06-12）：原结构轴越权——把 `node_path`/`node_type`/`level`/祖先链/`path_source` 等强行打在每个块上，而这些非"打目录标签"层能精准做好。改为：`StructuralAxis` 瘦身为**纯目录打标器**，每块只留可靠标签 `is_heading`/`text_level`/`is_toc`（+ `standard_id`/溯源），删标题栈与 `_process_heading`/`_annotate_content`；**条文号识别 + `node_type` + 建树 + 祖先链 + 引用图全部下沉进 `GranularityAxis`**（`classify_heading` 改自包含纯函数，`node_type` 由号段数自推）。`nodes.json` 形态不变，`03`/`04` 不受影响；`structure.json`（仅 02 自用）改为扁平目录标签块。⚠️ 待服务器重跑 02 对齐基线。
  - **T2 结构轴独立成文件 + catalog 升级值标签 + 目录定位（方案5）**（✅ 2026-06-13）：`StructuralAxis` 从 `02` 抽出为 `pipeline/structural_axis.py`（与建节点树解耦、可单测）。`is_catalog`(bool) 升级为 `catalog`(值)——块本身是目录页→`"目录"`，否则→**所属目录条目标题**（属于目录里哪一条），目录前/无目录→`None`。定位用**方案5（混合）**：目录页解析成有序条目表（骨架真值，兼容括号页码与点导引裸页码）→ 正文按文档序单调前瞻扫描、归一化匹配条目切换"当前条目"，条(x.x.x)不在目录则归属其节；无目录退化为以 `is_heading` 标题作边界。目录页识别改**区域判据**（连续成行 ≥`MIN_TOC_RUN` / 整列），孤立"行尾带数字"正文短行不误判（守"不做减法"）。建树层 `02` 跳过判据 `is_catalog` → `catalog=="目录"`。⚠️ 阈值/尾页码正则按常见排版设默认，待服务器跑真规范微调；建树层尚未改用 `catalog` 建树（仍走 `classify_heading` 号段路径）。另加 `catalog_source` 审计字段（`toc_page`/`toc_match`/`inherited`/`heading_fallback`/`none`），让方案5各定位来源在结果可见、`print_stats` 按来源分解计数。
  - **T2 删冗余透传 + is_heading 改用 text_level**（✅ 2026-06-13）：`FormatAdapter` 删 `raw`（整条 MinerU dict 冗余，需原件靠 `block_idx` 回查 `data/parsed/`；唯一读者 `strength._bold_from_raw` 本就拿不到、且强条已废弃）、`list_items` 展开后不再逐块保留（`_flatten` 里 `pop`）。`is_heading`(派生 bool) 改为 `text_level`(MinerU 标题层级原样透传，仅标题块有键)，消费方 `02` 建树判定 + 结构轴 `_locate`/`print_stats` 同步改 `text_level is not None`。审计「操作+改键」仅剩 `page_idx→page`(+1)、`table_caption→text`、`table_body→body` 三处实质转换（合理改名，与 is_heading「改名却丢原值」不同）。
  - **T2 拆 FormatAdapter + 结构轴更名 CatalogLabeler（术语统一）**（✅ 2026-06-13）：`FormatAdapter`（+ `_HTMLTableParser`/`_expand_spans`/`_html_table_to_rows`）从 `02` 抽到 `pipeline/format_adapter.py`（纯 stdlib、可复用、可单测，`02` 瘦身为「建树层 + 编排/CLI」）。**术语统一**：职责重划后「结构轴」已名不副实（只打目录标签、不建结构），故 `structural_axis.py`→`pipeline/catalog_labeler.py`、类 `StructuralAxis`→`CatalogLabeler`、中文表述「结构轴」→「目录打标器」（docstring/统计标题/`02` import 与引用同步；上文历史条目保留旧名以存真）。注意「结构**层**」仍指阶段1整层（打标+建树），未改。
  - **T2 拆建树器独立成文件 + `GranularityAxis` 更名 `TreeBuilder`（术语统一）**（✅ 2026-06-13）：建树逻辑（`GranularityAxis` 类 + `classify_heading`/`_infer_node_type`/`_parent_path`/`_resolve_parent` + 条文号/父路径正则）从 `02` 抽到 `pipeline/tree_builder.py`（可独立单测，`02` 彻底瘦身为「编排 + CLI」；至此结构层三件 `format_adapter`/`catalog_labeler`/`tree_builder` 各自成文件并列）。**术语统一**：`GranularityAxis` 双重失准——既属已废弃「三轴」旧模型，「granularity（粒度）」又已专指索引期树上视图（`view.py`，T7），与建树无关；故类 `GranularityAxis`→`TreeBuilder`、中文「建树轴/粒度轴」→「建树器」，`02`/`catalog_labeler`/`format_adapter` 的 import 与 docstring 引用同步（历史条目保留旧名存真）。行为保持：方法体逐字搬移，合成树用例验证 `classify_heading`/parent 反推/祖先链/引用图分型一致；`02` 不再 import `schema`/`extract.references`（已随建树器迁走）。
  - **T2 改用 catalog 建树（目录条目为骨架）·解决父链断裂**（✅ 2026-06-13，方案 B）：根因——旧建树 `_flush` 丢弃「只有标题没正文」的章/节骨架节点，致子条款号段反推父时找不到 → `parent_id=None`、祖先链空、small-to-big 失效。改 `TreeBuilder.apply` 为 **①目录条目物化骨架（恒存在，根治断裂）→ ②正文标题块并入同号骨架（接地：补 provenance）或建新条/款节点 → ③连边（号段为主、catalog 归属兜底）→ ④剪空正文叶（骨架恒留，级联到稳定）+ 祖先链 + 引用图**。条目嵌套（5.3 属 5）与条内层级（5.3.4.1 属 5.3.4）仍按号段（catalog 只定位到节深）；无目录页（`entries` 空）退化为「保留骨架 + 号段」best-effort。配套：`CatalogLabeler.annotate` 把有序条目表存到 `self.entries` 供建树取，`02.Pipeline.run`/preview 传 `entries=axis.entries`。合成用例验证：纯空骨架 `5.3` 存活、`5.3.4→5.3`/`5.3.4.1→5.3.4`、骨架被正文标题接地、空叶 `7.1.1` 剪除、无目录退化靠 catalog 兜底挂载、临时键 `_catalog`/`_skeleton` 清理。⚠️ **本地仅合成数据验证；目录解析质量（方案5 阈值）+ 真规范树形待服务器跑 GB 50016 对齐基线**——这是 B 路线的已知风险（强依赖目录解析）。
- [x] **T3 删强条排序**（✅ 2026-06-12）：`retrieval/engine.py` 去掉 `rerank()`/`search()` 里 `mandatory + non_mandatory[...]` 的强条置顶与 `vector_search` 的 `filter_mandatory`；结果纯按 RRF/rerank 排序后切 `top_k`。残留 `MILVUS_OUTPUT_FIELDS` 的 `is_mandatory` 与 stats 观测留 T4/T6 清理。
> T4/T5/T6 仍是原编号原职责，只是从"波1 拆强条收尾"重新归到下面的执行批次里——纯删字段动作（T4/T6）拆出来跟着第 1/3 步走，避免在新链路尚未打通时空删导致护栏更没得跑。

**第 1 步 — 接通最小可跑链路（T7 最小切片 + T8 免费表征 + T4），让护栏复活**（无新依赖；这是当前唯一阻塞项，先做）：

- [x] **T7（最小切片）粒度视图**（✅ 2026-06-13）：新增 `view.py` 的 `view(nodes, index_granularity) → 检索单元`（索引期纯函数，读 `nodes.json`），**先只做 `clause` 层 emit**（`node_type=="clause"`；section/paragraph 抛 `NotImplementedError` 留后补，bogus 值 `ValueError`）。`ParseProfile` 从 `02` 抽到可 import 的 `parse_profile.py`（数字前缀文件不可 import；命名避开 stdlib `profile`）：删 `chunk_granularity`/`enrichment`/`structure_depth`，加 `index_granularity`（section\|clause\|paragraph）+ `reprs`（list，缺省免费 4 项 `raw/sparse/dense/context_aug`）+ `small_to_big`；`terminal_stage` 改 PRD §3.2 值 `structure|reprs|index`。`02` 改 import + 同步 CLI（`--index-granularity` 替 `--chunk-granularity/--enrichment`，默认终止 `structure`）+ 删建树层对旧字段的 vestigial 引用。冒烟测试通过（default_factory 不共享、clause 选层正确、02 可加载）。待第 1 步 T8/T4 接 `view` 入索引。
- [x] **T8（免费 4 项）表征注册表**（✅ 2026-06-13，`reprs/`）：新建 `reprs/` 包——`__init__.py` 注册表 `REGISTRY`（ReprKind→产函数）+ `enrich(nodes, enabled)`/`attach(node)` 运行核心（原地给节点挂 `reprs`，未注册 kind 安全跳过=前向兼容）；四个免费表征各一文件：`raw`（节点 content 原文，返回用）/`sparse`（node_path+title+content 词项拼接，供 BM25）/`dense`（title+content 待嵌入正文）/`context_aug`（祖先链 ‖ 正文，small-to-big 入口）。**向量归属**：dense/context_aug 只产待嵌入文本，向量留索引期 04 用 embedding 模型统一算（模型唯一 owner 在检索栈，表征层不加载模型→仍属"免费"）。`context_aug` **复用 TreeBuilder 已算定的 `ancestor_titles`、不重算**（接管 `extract/ancestors.py` 职责；ancestors.py 与 build.py v1 逻辑随 T5 退役）。`DEFAULT_ENABLED` 与 `parse_profile.DEFAULT_REPRS` 一致。合成节点验证：4 项文本形态/空骨架退化（content 空时 context_aug 退为 title）/dense 无 vector/未注册 kind 跳过。`table_struct`/`modal`/`condition`/LLM(summary/questions) 推到第 4 步。
- [x] **T4 索引读 `nodes.json` + 去强条字段**（✅ 2026-06-13，`04_build_index.py`）：改读 `nodes.json` → `view(nodes, index_granularity)`（T7 选粒度）→ `reprs.enrich`（T8 挂表征）→ emit。**各表征明确消费方**：`sparse`→BM25 语料、`dense`→嵌入文本（向量）、`raw`→`content` 字段；引用扩展用 `references_to`（从节点 `references` 桥接出 strong/cross_standard 边的 `to`，供 engine 沿用 list[str] 口径）。行带 `node_id`/`parent_id`/`granularity`（`parent_id`=T9 small-to-big 锚点）；Milvus schema **删 `is_mandatory` 字段 + 其 INVERTED 索引**（加 `node_id` INVERTED），`metadata.json` 同步去字段。索引路径改 `data/vector_store/{standard}/{profile}/`（profile 隔离）；collection 用检索层共享 `config.collection_name(store_dir.name)` 推断，与 `07_eval`/`server` 一致→评测点 `--store-dir` 至本目录即同名零改动（⚠️ 多规范并存时 profile 名须含规范区分以免 Milvus collection 相撞）。**耦合改动**：`engine.MILVUS_OUTPUT_FIELDS` 删 `is_mandatory`、加 `node_id`/`parent_id`/`granularity`，`search` stats 去 `mandatory`（这是 T9 计划的清理，因「新 schema 删字段后 engine 仍 output 该字段会查询报错」属硬依赖故前移；T9 仍负责 small-to-big + ref-type 感知扩展）。`server.py` 的 `is_mandatory`/`mandatory` 全走 `.get` 不崩（清理留 T6）；`07_eval` 不读 hit 的 `is_mandatory`、按 `node_path` 集合判命中（clause 粒度下与包含关系等价）。合成数据验证 `node_to_row`/`_expandable_refs`/`save_metadata`（无 is_mandatory、references_to 为 list、dense 文本非空）；BM25/Milvus 路径依赖服务器服务，待里程碑跑。
- [ ] **🏁 里程碑（护栏复活）**：用新模型重建 GB 50016（服务器从 ce-code 根单入口跑 `python build.py all --input data/parsed/<std>/auto/<std>_content_list.json`），`python -m tools.eval --store-dir data/vector_store/<std>/<profile>` 与旧 v1 索引对比召回（基线口径已换，按 PRD §四**包含关系**判命中；clause 粒度下现有精确集合判命中等价）。**此步打通前，下面各步都不算有护栏。** ⚠️ 同时验 B 建树（catalog 骨架）在真规范上的树形 + `catalog_source` 分解。

**第 2 步 — T5 退役/重定位 `build.py`**（依赖第 1 步的 reprs runner 形态）：

- [x] **T5 删 v1 富化链**（✅ 2026-06-13）：固有事实（引用图/祖先链）已在 `02` 算定、表征 runner 职责由 `reprs.enrich`（T8）+ `04` 承担，故 `build.py`**直接删除而非重定位**——连同 `strength.py`（强条/语气 v1 逻辑，机制已废）、`ancestors.py`（祖先链已被 `tree_builder._attach_ancestors` 接管）一并删。`extract/__init__.py` 改为只 import `references`（引用图分型，建树期固有事实，仍被 `tree_builder` import），`extract/` 现仅剩 `references.py`。import 烟测通过。

**第 3 步 — T9 small-to-big + T6 服务层**（依赖第 1 步索引带 `parent_id`）：

- [ ] **T9 small-to-big 检索**（`retrieval/hybrid_retriever.py` + `retrieval/service.py`，承旧 `engine.py`）：细粒度命中后靠 `parent_id` 上探返回整条/整节；`modal` 作可选 filter 通道（query 带强制意图时启用，依赖第 4 步 modal 表征）。
  - [x] **去重键 node_path → node_id**（✅ 2026-06-13，small-to-big 前置）：`merge_results`/`expand_references` 改按 `node_id` 去重（clause 粒度下与 node_path 1:1 等价、行为保持；section/paragraph 粒度下 node_path 不唯一时唯 node_id 恒唯一）。`references_to` 仍存 node_path，故引用解析按 node_path 查 meta、去重按 node_id；跨规范引用查不到自动跳过。合成数据烟测通过。`get_clause` 保持 node_path 匹配（`/clause/{std}/{path}` 路径直取端点，契约即按路径）。`MILVUS_OUTPUT_FIELDS`/stats 的 `is_mandatory`/`mandatory` 残留已在 T4 清掉，本次同步清 `search` docstring 残留。
  - [ ] **small-to-big 上探**（待做）：命中单元靠 `parent_id` 回补父节点整条/整节上下文。
- [x] **T6 服务层清理**（✅ 2026-06-13，`service/server.py`）：删 `mandatory_clauses_count` 响应字段、`★强条`/`n_mandatory` 日志与 `强条=%s` 观测行。`/search` 返回挂 small-to-big 父节点上下文部分留 T9（依赖上探实现）。

**第 4 步 — 波2 表征补全 + 波3 LLM 表征 / 评测改造**（依赖前三步 + Qwen3）：

- [ ] **T8 表征补全**：`table_struct`（接管现表格 HTML 解析）/`modal`（复用旧 `strength.parse_modal_strength` 正则，删 `is_mandatory` 法律逻辑，产出 `feature` 的 `modal` 投影）/`condition` 谓词（`feature/condition.py`，抽不准标 `scope_status:unknown`）。
- [ ] **T10 评测换指标**（`tools/eval.py`）：删"强条召回率"首要指标，改 Recall@k / 引用召回 / MRR / 金标秩；按**包含关系**判命中（配合 small-to-big）。`tools/review_quality.py` 同步：删强条统计/误标检测，改节点树健康（孤儿节点 / 空内容 / 表格归属 / 悬空引用）。
- [ ] **LLM 表征**（`feature/summary.py`、`feature/questions.py`）：调 Qwen3 生成摘要 / 假设问题表征，入 `dense` 多通道。

**多规范扩展**：GB 50116（火灾自动报警系统）待收录。

---

## Phase C：造价知识底座（CostAgent / 算量组价 agent）（⬜ 待办）

> 对应 PRD §4 收录范围 / §3.3、`cost_agent_prd.md` 八 / `cost_agent_tech.md` 三、六，以及 CostAgent M0 数据底座里程碑。新增**关系库 + 知识图谱**两层与造价检索原语；与 Phase B（防火轨数据模型）解耦，可并行。
> 范围：**广东省深圳市·房建专业**先行（与 PRD v3 定位一致，组价用**深圳本地 2024 版消耗量标准**非省定额）；算量引擎/图纸解析/编排在任务层，不在此。

### 知识收录进度（对齐 PRD §4 收录范围；✅已解析 / ⏳已下载待解析 / ❌未收录）

> 必要性：⭐MVP（最小闭环必需）/ [必收] / [条件] / [可缓]。MVP 最小收录集 = PRD §7 五项。

| doc_id | 名称 | 必要性 | 收录 | 下一步 |
|---|---|---|---|---|
| GB-50500 | 建设工程工程量清单计价标准 | ⭐MVP | ✅ | **2024 版已无清单项目录**（搬到 50854）→ 不进 bill_spec；抽「综合单价/费用构成」规则表，35 张表归报表模板 |
| GB-50854 | 房屋建筑与装饰工程工程量计算标准 | ⭐MVP | ✅ | **已抽 bill_spec 472 项 + 5 辅助表、已入 PG**（见下）；计价口径由 50500 规则表旁挂，不并进 bill_spec |
| GB-50856 | 通用安装工程工程量计算标准 | [条件] 含机电 | ✅ | 含安装项目时入库 |
| SZ-SJG171 | 深圳市建筑工程消耗量标准 | ⭐MVP | ✅ | 电子表清洗入 `quota_item`/`quota_resource`（组价主体） |
| SZ-SJG170 | 深圳市土石方与地基基础工程消耗量标准 | [必收] 含基础/土方 | ✅ | 同上，土方/地基阶段 |
| SZ-FLBZ-2023 | 深圳市建设工程计价费率标准（2023） | ⭐MVP | ✅ | 费率入费用表（管理费/利润/安文/规费/税金） |
| SZ-JGXX-PRICE | 深圳市建设工程价格信息（月刊） | ⭐MVP | ⏳ 2026-05 已下载 | 解析 → `resource_price`（动态独立管道，带 `effective_period`） |
| SZ-ZPS | 深圳市装配式建筑工程消耗量标准 | [条件] 仅装配式 | ❌ | 装配式项目再收 |
| SZ-JXTB | 深圳市施工机械台班消耗量标准 | [条件] 信息价无台班价时 | ❌ | 按需收 |
| SZ-2024GZ-TZ | 2024 版清单计价标准贯彻实施通知及附件 | [可缓] | ❌ | 接 2024 消耗量↔2023 费率版本缝时收 |
| SZ-FBFX-FGBZ | 房建造价文件分部分项/措施项目划分标准 | [可缓] | ❌ | 组织口径，非取数源 |
| SJG46-2023 | 建设工程安全文明施工标准 | [可缓] | ❌ | 安文费已由费率计取，做法标准非取数源 |

### 数据资产（关系库优先）

- [ ] 关系库 PostgreSQL 建表：`bill_spec` / `quota_item` / `quota_resource` / `resource` / `resource_price` / `hist_bill`，**强制带 `doc_id` + `version` + `region`(深圳) + `effective_priority`(深圳本地=1)**，价格带 `effective_period`
  - [x] **PostgreSQL 16 已部署**（✅ 2026-06-16，服务器 stone）：因系统共用 docker 与满盘 `/` 不可动，**用 rootless docker 起独立 daemon**（caic 用户名下，data-root=`/mnt/nvme/calvin/docker/data`，与共用 daemon 零交集），`postgres:16` 容器 `ce-postgres`（端口 **5433**，库 `ce_cost`，用户 `cost`，卷 `ce_pgdata` 落 nvme，对内网开放）。部署细节见 DEV §6。`bill_spec` 表已建（code/name/unit/feature_schema JSONB/calc_rule/work_content JSONB/chapter/spec_version/provenance JSONB）**并已导入 GB/T 50854 的 472 条**。
  - [x] **DDL 落仓库可复现**（✅ 2026-06-16，`cost/schema.sql`）：把手敲的 `bill_spec` 表 + 其余表（`quota_item`/`quota_resource`/`resource`/`resource_price`/`hist_bill` + `aux_table`）一次建齐，全部带治理字段 `doc_id`/`spec_version`/`region`/`effective_priority`（价格 `resource_price` 带 `effective_period` DATERANGE + `btree_gist` EXCLUDE 防时效重叠）；幂等 `IF NOT EXISTS` + 索引。**待办**：服务器跑一遍对齐已建 `bill_spec`（手敲版缺治理字段，DDL 已补；列差异需 ALTER 对齐）。
  - [x] **可复现导入脚本**（✅ 2026-06-16，`cost/load_pg.py`）：替代手敲 staging+`\copy`，psycopg 直连 `localhost:5433/ce_cost`，读 jsonl 按主键 `ON CONFLICT DO UPDATE` 幂等 upsert；`--init-schema` 先建表；`_backfill_doc` 兼容归一前旧 jsonl。⚠️ 服务器装 `uv add 'psycopg[binary]'` 后跑（本地 torch cu121 阻塞 sync，仅 py_compile + 纯函数验证）。
- [ ] 清单计量规范结构化入 `bill_spec`（复用 MinerU 解析 + 规则，含 calc_rule + feature_schema）
  - [x] **GB/T 50500-2024 已解析**（chunks.json 146 节 + 35 表）。**核实结论（2026-06-16）：2024 版规范重构后 50500 已无清单项目录**——清单项+编码+计算规则整体搬到计算标准系列（50854 等），50500 只剩计价规则正文 + 附录 E/F/G 标准表格。全 35 表为**空白报表模板**（E.2 等「项目编码」列只是表单列头，无 9 位编码数据行，全表仅 1 行正文偶含编码）。故**旧「从 50500 抽 bill_spec / 按 code 合并计价口径」模型作废**（基于 2013 版假设，那时 50500 自带清单附录）。重定方向见下「GB 50500 计价口径」条。
  - [x] **GB/T 50854-2024 已抽 `bill_spec`**（✅ 2026-06-16，`cost/bill_spec.py`）：chunks.json → 双出口 `data/structured/bill_spec.jsonl`（**472 清单项**，编码无重复/无断号，feature_schema + work_content 编号拆 list）+ `aux_tables.jsonl`（5 辅助表：土/岩石分类、工作面宽度等，原样留 body 供 calc_rule 查表）。判别：表头含「项目编码」→ bill_spec，否则 → aux；列名别名（措施项目「单位」/7 列「项目特征描述」）；每条带 provenance 回指原表。质量门禁留人工抽查 4 处（3 模板项源表无特征属合法 / 1 行 `010102007` 源表单位格丢失，按不杜撰未猜填）。**已导入 PG**（✅ 2026-06-16，`ce_cost.bill_spec` 472 条，走 staging 表 `\copy`（控制字符当 quote/delimiter 整行读 jsonb）+ `INSERT ... j->>` 展开）。**spec_version 已归一 + 加 doc_id**（✅ 2026-06-16，`normalize_spec` 把文件名 basename → canonical `GB/T 50854-2024` + `doc_id=GB-50854`，bill_spec/aux 两出口都带）。**待办**：服务器重跑 `python -m cost.bill_spec` 让 jsonl 带新字段（或靠 `load_pg._backfill_doc` 兜旧 jsonl）→ `load_pg --aux` 把 5 张 `aux_table` 入库。（注：原"按 code 合并 50500 计价口径"已作废，50500 无可 join 的 code，见上条 + 下「GB 50500 计价口径」条。）
- [ ] **GB 50500 计价口径**（替代作废的"抽 bill_spec"，2026-06-16 重定）：
  - [x] ① **从 50500 正文抽费用构成规则表**（✅ 2026-06-16，`cost/price_composition.py`）：声明式规则（RULES：node_path + 正则）锚定 50500 原文单句、正则抽构成项、宁缺毋造（不中即报错），产 `data/structured/price_composition.jsonl`（10 行）：**综合单价（2.0.9）= 人工费/材料费/施工机具使用费/管理费/利润/一定范围内的风险费用（不含增值税）**；**工程造价（3.1.2）= 分部分项/措施项目/其他项目/增值税**（核实：2024 版**四部分**，规费已并入、非旧 2013 的"规费+税金"五部分）。每行带 provenance（node_path+clause）+ doc_id/spec_version；`schema.sql` 加 `price_composition` 表（治理字段全 + `UNIQUE(doc_id,composite,seq)` 幂等键），`load_pg.py` 加 `--price-composition` loader。**不并进 bill_spec**。⚠️ 本地仅 py_compile + 纯函数验证（无 click/torch），服务器 `python -m cost.price_composition` + `load_pg --price-composition` 实跑入库。
  - [ ] ② 35 张附录 E/F/G 表 = 计价/结算**报表模板**，归任务层 CostAgent 报表生成，不在知识层 `cost/` 处理。
  - [ ] ③ 合同/计量/支付/索赔条文优先级低于组价取数，需要时走规范 RAG 条文检索管道（与防火规范同一套，不单独建库）。
- [ ] **深圳消耗量标准**导入 `quota_item` + `quota_resource` + `resource`（SJG 171-2024 主体 + SJG 170-2024 土方/地基，电子表清洗；标注 `region=深圳`/`effective_priority=1`）
- [ ] 价格库导入 `resource_price`（深圳信息价 SZ-JGXX-PRICE，带 `effective_period` 时效，**走动态独立更新管道**）
- [ ] 费率标准（SZ-FLBZ-2023）入费用计算表（企业管理费/利润/安文费/规费/税金）
- [ ] 历史工程库 `hist_bill`（脱敏 + 质量标注，供相似案例对标；[可缓]）

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

**模型/部署待评估项**：造价轨 embedding 用 BGE-M3 vs 复用规范轨 bge-large-zh-v1.5 是否统一为单服务（见 DEV.md §2.4/§3.3 造价轨实现）。
