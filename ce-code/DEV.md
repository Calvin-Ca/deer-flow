# 知识库模块 DEV Doc

> **文档定位**：本文回答"知识库如何设计与实现、为什么这样选、检索质量如何度量"。
> 与 PRD 的边界：PRD 定义"要什么、业务上算合格"；本文定义"怎么做、技术上算合格"。
>
> **核心价值在"决策记录"**：配置参数看代码就知道，本文要记录的是"为什么这样选"，让半年后的你或新人不重复踩坑。
>
> **范围**：知识层是**算量组价的造价知识底座**（PRD），**定位为广东省深圳市·房屋建筑专业**，按两类知识组织——**规范类知识**（清单/计量规范 PDF，服务"算量"）与**结构化造价数据**（深圳本地定额/价格/费率/历史，服务"组价"）。
> **进度与待办见 `TODO.md`**，本文不重复列。
>
> **两条贯穿全文的硬约束（PRD §4/§5 落到技术）**：
> - **地区强隔离**：只收录、只召回深圳本地现行有效标准（深圳有独立 2024 版消耗量标准，与省内其它城市口径不同），地区元数据是过滤的硬条件，隔离失败 = 召回错误。
> - **时效性入库即校验**：所有收录单元入库前已核对实施状态，**旧版本 / 废止单元不入库、不保留**——故检索侧**不再做废止 / 过渡期过滤**，节点级 `status` 仅作溯源标注（库内恒为现行有效）。

---

## 1. 整体架构

> 一张图说明数据如何从原始知识源流向可检索状态。
> 知识层 = **数据 + 检索**，不含生成/编排（属任务层 ce-services）。retrieval + rerank 模型只在此加载一份（唯一 owner）。

知识层按两类知识、两套机制组织，共用同一检索接口对外：

| 知识类别 | 服务环节 | 数据来源 | 核心载体 | 对外原语 | 状态 |
|---|---|---|---|---|---|
| 规范类知识 | 算量（应该算什么/如何算） | 清单 GB 50500 / 计量 GB/T 50854（PDF） | MinerU → 节点树 → Milvus + BM25 | `/search` `/expand` `/clause` | 引擎已建（POC 验证） |
| 结构化造价数据 | 组价（人材机 + 价格） | 深圳消耗量标准(SJG 171/170-2024) + 信息价(月刊) + 费率标准(2023) + 历史项目 | PostgreSQL（单一事实源）+ KG + Milvus | `/bill/match` `/price/compose` `/quota` | 待建 |

**规范类知识入库流水线（分阶段，每阶段读上一阶段产物、写自己的产物）：**

```
PDF（清单/计量规范）
      │
      ▼
[阶段0] MinerU 解析 ──► [阶段1] 切分建树 ──► [阶段2] 挂表征 ──► [阶段3] 索引
  -m parser           build(structure)      build(reprs)      build(index)
  parser/(mineru)     splitter/(toc)        feature/(免费4项) index/(view+各index)
      │                    │                                       │
      ▼                    ▼                                       ▼
 data/parsed/        chunks.json(唯一真值)                 BM25 + Milvus + 元数据
 (不可变缓存,只跑1次)  +引用图+祖先链(固有事实)               data/vector_store/{std}/{profile}/
                                                                  │
                                                                  ▼
                                                    ┌──────────────────────┐
                                                    │ service/knowledge_api│
                                                    │  检索原语（对外契约） │
                                                    │  FastAPI :8100       │
                                                    └──────────────────────┘
```

> **关键设计**：节点树（阶段 1）一次建好，**粒度是索引期（阶段 3）在树上选的视图，不是切树**。换粒度/换表征只重跑下游，不重跑 MinerU（最贵，约 60% 耗时）。结构（建树）/ 表征（多投影）/ 粒度（树上视图）三件事正交分离。

> **分层重构（2026-06-15）**：各阶段数据统一为显式 IR（`ir/` 的 `@dataclass` + `to_dict/from_dict`：Document / Chunk / ChunkFeature / RetrievalQuery / RetrievedChunk / KnowledgeContext），各层做成「基类 + factory + 多策略」可插拔（★实装/◌占位）：解析 mineru★·unstructured◌；切分 toc★·semantic◌·tree◌；表征 raw·bm25·dense·context_aug★·keyword◌·graph◌；索引 bm25·vector·metadata★·graph◌；检索 dense·bm25·hybrid★·graph◌。`nodes.json`→`chunks.json`；旧 `reprs/`→`feature/`、`retrieval/{engine,indexer,config,server}` 拆入 `index/`+`retrieval/`+`service/`+根 `config.py`；旧 `node_id` 废除，全层以 `node_path` 为键。对外 :8100 契约逐字不变。

**对外接口契约**（上游模块依赖的唯一边界）：

```
输入（POST /search）：
  query   : str          查询文本
  intent  : str          clause_lookup | cost_match —— 策略选择参数
  k       : int          返回条数（上下文窗消费预算，业务层设定）
  profile : str          parse_profile 名（同一 query 打不同索引做 A/B，可选）
  filter  : standard / version / region（元数据过滤，可选）

输出：ranked results[]，每条含：
  node_path / standard_id / version          （node_path 即节点 id；旧 node_id 已废）
  title / content（命中节点正文）
  ancestor_titles（祖先链，溯源用）
  references（引用边）
  provenance（回指 MinerU 原始块：source_file / block_idx / page）
  score（RRF / rerank 相关度）
```

> 上游模块只依赖此契约。下游验收时可用 mock 的"理想检索结果"独立测试上游逻辑。
> **职责边界**：业务层只做三件事——① 选 intent；② 业务对象 → query + filter；③ 设 k。"query 进 → ranked results 出"全包在知识层，检索策略不下放。

---

## 2. 数据处理流水线（规范类知识）

### 2.1 解析（Parsing）

- **工具与版本**：MinerU **3.2.1**（远程 API）。配套 mineru-vl-utils **1.0.2**。
- **处理的格式**：PDF（清单/计量规范）。版面/标题/表格/公式抽取。
- **部署方式**：阶段0 **只走远程 MinerU API**（`172.19.2.2:8000`，常驻热服务，单页 ~1.8s，整本一次解析无本地 OOM，调用方零 GPU/MinerU 依赖）。
### 2.2 切分（Chunking）★

> 切分策略直接影响检索效果，是最重要的决策点之一，务必记录依据。
> **本项目语境下"切分"= 建节点树**，而非传统的定长切块——见决策依据。

- **切分策略**：**以 PDF 文档原生目录（TOC）为骨架还原成节点树**（`splitter/toc_splitter.py` 的 `TocSplitter`，当前默认/唯一；目录打标/建树/引用图分型三件 2026-06-15 已合并入该单文件）。目录条目先**物化为骨架节点**（恒存在），正文块再按条文号号段 / 目录归属挂载到骨架下。**切分深度可控（2026-06-15）**：`split` 接 `max_depth` + `subsplit` 两个**切分期**参数（profile 的 `toc_max_depth` / `subsplit` 透传）——`toc_max_depth` 决定建到第几级目录（1=章/2=节/3=条…，None=全目录深度），超过该层级的目录条目/标题不单独建节点、正文并入最近祖先；`subsplit` 决定目录层**之下**是否按编号细分：`none`（树严格镜像目录，不细分）/ `number`（按编号号段再切出更细的编号子节点，节/条/款/项皆可，不专指条）。二者正交，替代了旧「建树/建条解耦 + 未实装 ClauseSplitter（`clause_strategy`）」的过渡设计。**切分深度 ≠ 索引粒度**：`toc_max_depth`/`subsplit` 决定树里**存在到哪一层**（结构），索引期的 `index_granularity`（`view(chunks, granularity)`）则在已建好的树上**选哪层 emit**（视图），换深度只重跑切分、换粒度只重跑索引。**层级用 `level`（还原出的目录树深度，1-base；索引行/HTTP 契约里出字段名 `node_level`，故意不改名）表达，不绑定固定档名**（2026-06-14：不再假定文档一定有"章/节/条"，造价定额表等套不上）；`chunk_type` 是与深度正交的**种类**（`container` 容器 / `leaf` 叶检索单元，建树末纯按"有无子节点"判定；"是否附录"等语义按需读 node_path 前缀，不进 chunk_type）。**深度自适应**（目录列到哪深就到哪），中间层级缺节点时 level 按真实父链算。无目录页时退化为复用 MinerU 标题层级 best-effort。
- **chunk 大小 / overlap**：**不适用固定长度**。粒度是索引期在树上选的视图（`view(tree, level)`），当前仅 `clause` 层已实现；small-to-big 检索期靠 `parent_id` 上探回补整条/整节上下文。
- **决策依据**：
  ```
  决策：以原生目录为骨架建树，而非按字符定长切分
  对比方案：① 按字符切分 ② 按字重启发式判层级 ③ 按原生目录建树（选定）
  结果：清单/计量规范自带清晰目录，目录是文档的"结构真值"——目录骨架法
        比字符切分/字重启发更可靠，且深度自适应；条款层级/交叉引用一旦按
        字符切分即被破坏，而"算什么/如何算"的取数依赖完整层级与引用图
  代价：强依赖目录解析质量（方案5 混合定位的阈值需在真规范上微调）；
        无目录页时退化为 MinerU 标题层级 best-effort
  底线：带目录的规范 PDF 首选 toc；定额电子表（无目录、表格为主）走结构化
        入库（§3.3），不套 toc——切法可插拔，按数据形态选
  ```
- **可插拔设计**：切分做成「基类 + factory 注册表」（`splitter/base.py` 的 `Splitter` 基类 + `splitter/factory.py` 的 `REGISTRY`/`select`），`profile.structure_strategy` 决定本次切法（缺省 `toc`）。换切法 = 换 splitter = 不同 profile = 隔离索引，可直接 ablation 对比召回。（parser / feature / index / retrieval 各层同构：base + factory，见各层 `factory.py` / `feature/pipeline.py` 的 REGISTRY。）

### 2.3 元数据设计

> 每个节点携带的元数据，直接支撑 PRD 的可溯源红线与检索过滤。
> 完整 schema 见 `ir/chunk.py` 的 `Chunk`（旧 `core/schema.py` 的 `Node` TypedDict 已废，重构为 `@dataclass`）。

| 字段 | 类型 | 用途 | 是否支撑业务规则 |
|---|---|---|---|
| `node_path` | string | 节点稳定 id（`chunk_id ≡ node_path`）：条文号路径（`1.0.3`），无编号则标题路径（`附录E`/`前言`） | 去重键 / 引用图锚点 / `/clause` 直取（旧 `node_id` 已废，全层以 node_path 为键） |
| `doc_id` | string | **知识库内部稳定标识**（`GB-50500`/`SZ-SJG171`），入库/检索/溯源以此为准，与 `standard_id` 解耦 | 标准编号可改版 / 待补号时锚点不变（PRD §4） |
| `standard_id` | string | 标准编号（`GB/T 50500-2024`/`SJG 171-2024`，可能"待补号"） | 支撑多规范召回、溯源展示 |
| `region` | string | 适用地区（`深圳`） | **地区强隔离的硬过滤键**（深圳 ≠ 省内其它城市） |
| `discipline` | string | 适用专业（`房建`） | 房建专业过滤 |
| `effective_priority` | int (1~4) | **效力优先级**：深圳本地=1（最高）→ 国标=4（最低，越具体越优先） | **口径冲突时取值排序**（PRD §4 元数据治理） |
| `is_dynamic` / `update_freq` | bool / string | 是否动态数据 + 更新频率（信息价月更） | 动态数据走独立更新管道，不参与口径优先级排序 |
| `version` / `effective_date` / `status` | string | 节点级版本 / 实施日期 / 时效状态 | 库内恒为现行有效（入库即校验）；`status` 仅作溯源标注，**检索侧不做废止过滤** |
| `level` | int | 还原出的目录树深度（1-base，根=1；行字段名 `node_level`） | 溯源展示、层级语义 |
| `parent_id` / `children_ids` | string / list | 树形结构 | **粒度视图 + small-to-big 全靠它** |
| `ancestor_titles` / `ancestor_paths` | list | 祖先链（建树时一次算定） | 支撑溯源、context_aug 拼接 |
| `references` / `referenced_by` | list | 引用边分型（strong/weak/exclude/cross_standard）+ 反向边 | **引用图扩展核心**（GraphRAG 底座） |
| `provenance` | dict | 回指 MinerU 原始块（source_file / block_idx / page） | **可溯源底线（PRD 核心原则）** |
| `node_path_source` / `node_path_confidence` | string / float | 路径来源审计（number/text_level/inherited/synthesized） | 低置信进抽查 |
| `features` | dict | 多表征投影（`dict[kind, ChunkFeature]`，见 2.4 + §4；旧 `reprs` 已改名） | 多通道召回 |

> **可溯源是底线**：任何节点都必须能回指其 MinerU 原始块（`provenance.block_idx → data/parsed/` 不可变缓存）。原始内容只读、不可变，派生物只持轻量指针。**不得因任何检索/表征优化被牺牲。**

### 2.4 向量化（Embedding）

- **模型与版本**：**当前用 bge-large-zh-v1.5**（POC 已部署，沿用至清单/计量规范入库）。
- **维度**：dim=1024，max_len=512。
- **部署位置**：服务器 `http://localhost:8097`，model_id `/model`，OpenAI 兼容接口。
- **向量归属约定**：表征层（`feature/`，旧 `reprs/`）只产**待嵌入文本**（`dense` = title+content，`context_aug` = 祖先链‖正文）；**向量由索引期 `index/`（`index/vector_index.py`，旧 `retrieval/indexer`）用 embedding 模型统一算**——模型唯一 owner 在检索栈，表征层不加载模型（故仍属"免费"表征）。
- ★ **选型理由 / 待评估**：
  ```
  现状：沿用 bge-large-zh-v1.5（中文通用召回稳定，POC 已验证）
  待评估：清单/定额/工料机是造价专业术语，分布与通用语料不同；
          BGE-M3（原生 dense+sparse 混检）可能更适配清单匹配——
          切 BGE-M3 需重建索引、换服务，列为评估项，未定。
          若切，规范类与结构化造价数据可合并为单一 embedding 服务。
  ```

---

## 3. 存储设计

### 3.1 向量库（规范类知识）

- **选型**：Milvus（`http://localhost:19530`，MilvusClient API）。
- **client 版本**：pymilvus **3.0.0**（MilvusClient API；ORM-style 已弃用）。
- **Collection / 索引结构**：按 `{standard}/{profile}` 隔离，collection 名由 `config.collection_name(store_dir.name)` 推断（`tools/eval.py`、`service/knowledge_api.py`、`index/` 共享同一推断，零改动对齐）。⚠️ 多规范并存时 profile 名须含规范区分，避免 collection 相撞。
- **索引行字段**（`index/manager.py` 的 `chunk_to_row`，Milvus/BM25/metadata 共用）：含 `node_path`（去重键 / 直取锚点）/ `parent_id`（small-to-big 锚点）/ `granularity` / `references_to` 等（字段名逐字保持旧契约，`ce-services` 经 /search 读这些名）。
- ★ **索引结构选型**：
  ```
  决策：节点级版本/效力字段（status/version）+ node_path 建 INVERTED 索引
  依据：元数据过滤优先于向量排序（先按 standard/version/region filter 再 rank），
        故过滤字段需可高效命中；node_path 作去重键与直取锚点亦需 INVERTED
  备注：向量索引类型（HNSW vs IVF）按数据量与延迟权衡，规模尚小，
        单规范约百~千条量级（GB/T 50500-2024 ~561 条），延迟非瓶颈
  ```

### 3.2 关键词索引

- **方案**：BM25（rank-bm25 库）。
- **语料来源**：`feature` 层的 `sparse` 表征（`feature/bm25.py`，旧 `reprs.sparse`）= node_path + title + content 词项拼接。
- **用途**：补充向量检索的**精确匹配能力**——条文号（"1.0.3"）/ 清单编码 / 专业术语精确召回，这是纯向量召回的短板。

### 3.3 关系/图谱 + 结构化造价数据（组价核心，待建）

> 规范类知识解决"算什么/如何算"；组价取数靠结构化造价数据 + 关系约束，**能算的不交给模型猜**（PRD 核心原则）。关系库为单一事实来源，KG 由其派生，向量库为语义补充。
> **实现顺序**：关系库建表 → 数据入库 → 跑通取数路径 → 加向量召回 → 加 KG 多跳。

- **引用图（规范类，已实现）**：不另起图库，引用边作为**节点固有事实**落 `chunks.json`（旧 `nodes.json`；`references` 分型 + `referenced_by` 反向边，建树期 `splitter/references.py` 一次算定）。检索期沿 strong 边强制扩展。引用图即规范的知识图谱、GraphRAG 底座。
- **关系库 PostgreSQL（单一事实源）**：`bill_spec`（清单规范，9 位统一编码 + calc_rule + feature_schema + spec_version）/ `quota_item`（定额子目，region + base_price + 人材机费；MVP 取**深圳市建筑工程消耗量标准 SJG 171-2024**，非省定额）/ `quota_resource`（定额→资源含量）/ `resource` + `resource_price`（资源价格，带 `effective_period` 时效）/ `hist_bill`（历史工程，脱敏 + 质量标注）。**所有表强制 `version` + `region` + `effective_priority`**（深圳本地=1）。**动态数据（信息价月更，`SZ-JGXX-PRICE`）走独立更新管道**：`resource_price` 带 `effective_period`，按 region+时效取价，不参与口径优先级排序。DDL 详见 `cost_agent_tech.md`。
- **知识图谱**：**P0 用 PG 关联表模拟**（`component_bill_map` MAPS_TO / `bill_quota_map` APPLIES / `quota_resource_detail` CONSUMES），数据量小时 PG join 够用，**P1 再迁 Neo4j** 评估多跳遍历性能。
- **造价向量库**：新建 `bill_spec_kb` collection（评估 BGE-M3 dense+sparse），复用同一 Milvus 实例；向量化内容 = 清单条目名称 + 特征描述 + 工程做法说明。
- **数据入库方式**：
  - GB 50500 / GB/T 50854：复用规范类流水线产物（节点树 → 规则抽取 `bill_spec` 字段入库）。
  - 定额电子表：CSV 清洗 → `quota_item` + `quota_resource` + `resource`。
  - 信息价：定期抓取/导入 `resource_price`，带 `effective_period`。

---

## 4. 检索策略 ★

> `retrieval/hybrid_retriever.py` 实现混合检索（BM25 + 向量 + RRF + 引用扩展 + rerank；旧 `retrieval/engine.py`），单路 `bm25_retriever.py` / `dense_retriever.py`，RRF 与引用扩展纯函数在 `retrieval/rrf.py`；权重与超参（依赖地址 / 模型 / top_k）在根 `config.py`（旧 `retrieval/config.py`）。

### 4.1 召回方式

- **规范类 — 混合检索 + 引用扩展 + rerank**：
  ```
  query
    → BM25（条文号/清单编码/术语精确匹配）
    → 向量（bge-large-zh-v1.5 语义召回）
    → 元数据过滤（region 硬隔离 + standard/discipline，先圈范围再排序）
    → RRF 合并去重（按 node_path）
    → 引用图扩展（strong 边强制拉取；weak 可选；exclude 禁止扩展）
    → rerank（cross-encoder 精排）
    → 口径冲突按 effective_priority 取值（深圳本地=1 优先于国标=4；动态价格不参与）
    → small-to-big 回补（命中块 + 完整条/节上下文 + ancestor_titles）
  ```
- **通道权重按 intent 调**（`/search` 接收 `intent` 参数）：

  | intent | 场景 | 主通道 | 召回-精度取舍 |
  |---|---|---|---|
  | `clause_lookup` | 算量取数（查清单/计量规范条款） | 向量 + BM25 均衡，引用扩展开启 | 高召回 + 高精度（控噪） |
  | `cost_match` | 清单匹配 / 组价（待建） | KG 收窄 + 混检 | 候选集高命中，LLM 候选内择优 |

- **权重设计依据**：
  ```
  向量是"贡献者、非承重墙"：清单匹配靠"清单编码/术语精确召回 + KG 关系
  约束"收窄候选，向量召回占比按 intent 调；向量库的构建属"知识表示"
  （离线调优、全业务共享，不按消费者重建），占多大权重属"检索策略"。
  ```

- **造价轨两条检索路径（待建）**：
  ```
  ① 清单匹配：混检在 bill_spec_kb 取 Top-K → KG MAPS_TO 收窄 → LLM 候选内择优
                → 输出 12 位编码（前9位规范 + 后3位顺序码）+ 特征描述 + 置信度
  ② 组价取数：KG APPLIES 取定额 → CONSUMES 取工料机含量 → resource_price
                按 region+时效取价 → 返回工料机清单（综合单价公式在业务层算）
  ```

### 4.2 业务规则的技术实现

| PRD 业务规则 | 技术实现 |
|---|---|
| 可溯源 | 每个返回节点带 `provenance`（source_file/block_idx/page）回指 MinerU 原始块；条款/页码出处随结果返回 |
| 无结果不能编 | 向量未命中 → BM25 兜底；仍无 → 返回父级章节而非杜撰 |
| 引用扩展默认开启 | 命中节点的 `references` 中 `type=strong/cross_standard` 边无条件拉取（`retrieval/rrf.py` 的 `expand_references`），weak 可选，exclude 禁止正向扩展 |
| 元数据过滤优先于向量排序 | 先按 `region`(硬隔离)/`standard`/`discipline` filter 圈定范围，再在范围内 RRF/rerank |
| 地区不串库 | `region` 为入库即标注的硬过滤键；查询声明地区 → 只召回同地区单元，跨地区单元（省内其它城市定额）零混入 |
| 版本/时效 | 库内只保留现行有效单元（入库即校验，旧版本/废止不入库）；节点级 `status`/`version`/`effective_date` 仅作溯源标注，**检索侧不做废止/过渡期过滤** |
| 口径冲突取值 | 同一类知识多源命中时按 `effective_priority`(1~4) 排序：深圳本地=1 优先于国标=4；动态价格走独立管道不参与 |
| small-to-big 上探 | 细粒度命中后靠 `parent_id` 上探返回整条/整节（**去重键已切 node_path ✅，上探回补待做**） |
| 能算的不交给模型猜 | 数值走业务层确定性公式；LLM 仅在检索 + KG 限定的候选内择优（清单匹配/组价取数） |

---

## 5. 检索质量度量 ★

> ⚠️ 边界说明：本节是**技术验收**，与 PRD 的业务验收解耦。
> 这些指标在**检索接口处闭环**，不依赖任何下游模块表现。**端到端任务指标归业务层。**

### 5.1 评估方法

- **评估集**：`data/eval_set/`（入 git）。
  - 现有：GB 50016 评测集 `gb50016_eval.json`（45 条，POC 期建，验证检索引擎）。
  - 待建：清单/计量规范评测集；清单编码匹配 `match_gold.jsonl`（构件→编码标注，指标 Top-1 ≥ 85% / Top-3 ≥ 95%）。
  - 单条格式：`{ "query": "...", "expected": ["GB 50500-2024 1.0.3", ...], "intent": "clause_lookup" }`。金标由业务层参与定义（"做成我的任务，你必须捞出这些"）。
- **评估工具**：自建脚本 `python -m tools.eval`。`--store-dir data/vector_store/<std>/<profile>`，每个 profile/intent 跑同一评测集，指标记一张表对比。**一次只动一个变量。**
- **判命中口径**：⚠️ **按包含关系判命中**——返回块**包含或等于**目标条即算命中（非严格 `node_path` 相等），否则只到结构/粗粒度的 profile 会被系统性低估，ablation 结论失真。clause 粒度下现有精确集合判命中与包含关系等价。

### 5.2 指标与目标

| 指标 | 含义 | 目标 | 备注 |
|---|---|---|---|
| Recall@k | 正确条文/编码出现在前 k 的比例 | 首要指标 | k 取业务约定值 |
| MRR / 金标秩 | 排序敏感度 | — | 避免"金标排第 95 位也算召回" |
| 引用条款召回率 | 被引用的关联条款是否被拉取 | — | 检验引用图扩展 |
| 清单候选集命中率 | 构件对应正确清单项在候选内 | 支撑业务红线 | 造价侧，待建 |
| 定额套用准确率 | 清单→定额关系正确性 | 支撑业务红线 | 造价侧（KG 正确性），待建 |
| 检索延迟 (P95) | 不含下游生成 | 待定 | 规模尚小非瓶颈 |

> **业务层端到端红线**（知识层需支撑，非自身指标）：编码 Top-1 ≥ 85%、组价准确率达标；红线内只建议不定稿，必须经 HITL 人工确认（在业务层）。

> ⚠️ **当前护栏现状**：流水线后半段曾与 `chunks.json`（旧 `nodes.json`）脱钩，`tools/eval.py` 一度打的是陈旧索引。**新树端到端可建索引、可被 eval 与基线对比** 是当前阻塞里程碑（见 `TODO.md`）。此步打通前"每步过护栏"的纪律无所附丽。

---

## 6. 依赖服务清单

> 只列依赖的**外部服务/设施**（数据库、模型、解析服务）及地址；Python 包不在此列。

### 已部署（规范类知识）

| 角色 | 服务/模型 | 地址 | 用途 |
|---|---|---|---|
| Embedding | bge-large-zh-v1.5 | `http://localhost:8097` | 条款向量化、query embedding（dim=1024，max_len=512） |
| 向量库 | Milvus | `http://localhost:19530` | 向量存储与检索 |
| VLM | Qwen2.5-VL-7B | `http://localhost:8098` | PDF 解析时图示理解 |
| 文本推理 | Qwen3-8B | `http://localhost:8099` | 查询改写、引用图 LLM 校验（`/no_think` 禁思考链） |
| PDF 解析 | MinerU API | `http://172.19.2.2:8000` | PDF 解析（默认远程，hybrid-auto-engine） |
| 知识服务 | 本模块（FastAPI） | `http://localhost:8100` | 对外检索原语 `/search` `/expand` `/clause` `/health` |

### 待部署（结构化造价数据 / 组价）

| 角色 | 服务/模型 | 地址 | 用途 |
|---|---|---|---|
| 关系库 | PostgreSQL | 待部署 | 清单/定额/价格/历史精确查询（单一事实源，version + region） |
| KG（P0） | PG 关联表（P1 迁 Neo4j） | 同上 | 构件→清单→定额→工料机多跳关系 |
| 造价 Embedding | BGE-M3（评估中） | 待部署 | dense+sparse 混检；是否与规范类合并单服务待定 |
| 造价向量库 | Milvus（复用实例） | `http://localhost:19530` | 新建 `bill_spec_kb` collection |

---

## 附录：关键决策速查

> 把全文带 ★ 的决策汇总于此，便于快速回顾"为什么当初这么定"。

| 决策点 | 选择 | 一句话依据 |
|---|---|---|
| 切分策略 | 原生目录（TOC）为骨架建节点树（`toc` splitter） | 目录是规范 PDF 的"结构真值"，比字符切分/字重启发可靠且深度自适应；按字符切分会破坏条款层级与交叉引用 |
| 粒度模型 | 索引期在树上选视图（`view`），非切树 | 三件正交（结构/表征/粒度），换粒度只重跑阶段 3，不重跑最贵的 MinerU |
| embedding 模型 | 现 bge-large-zh-v1.5；造价语料评估 BGE-M3 | POC 已验证沿用；造价术语分布不同，BGE-M3 混检待评估 |
| 向量索引归属 | 向量由索引期 `index/vector_index.py`（旧 `indexer`）统一算，表征层只产待嵌入文本 | embedding 模型唯一 owner 在检索栈，表征层不加载模型 |
| 引用图存储 | 落 `chunks.json` 作固有事实，不另起图库 | 引用图即规范知识图谱/GraphRAG 底座，建树期一次算定 |
| 组价数据底座 | 关系库（PG）单一事实源 + KG 派生 + 向量补充 | 能算的不交给模型猜；清单↔定额多对多，纯向量不够，需关系约束 |
| KG 落地 | P0 用 PG 关联表，P1 迁 Neo4j | 数据量小时 PG join 够用，先跑通取数路径再上图库 |
| 判命中口径 | 按包含关系（非严格 node_path 相等） | 否则粗粒度 profile 被系统性低估，ablation 失真 |
| 可溯源 | 每节点 `provenance` 回指 MinerU 原始块，不可牺牲 | 改算法重派生/人工核对/PDF 高亮/出处可查全靠它（PRD 核心原则） |
| 时效性 | 入库即校验，旧版本/废止不入库；检索侧不做废止过滤 | 库内恒现行有效，`status` 仅作溯源标注，省掉检索期过渡期/废止过滤复杂度 |
| 地区隔离 | `region` 作硬过滤键，只收只召深圳本地 | 深圳有独立 2024 消耗量标准，最易串库；隔离失败=召回错误（PRD §5 红线） |
| 效力优先级 | 元数据 `effective_priority`(1~4)，深圳本地=1 → 国标=4 | 口径冲突时确定性取值排序，越具体越优先；动态价格独立管道不参与 |
| 稳定标识 | `doc_id` 为库内锚点，与 `standard_id`(标准编号) 解耦 | 标准编号可改版/待补号，`doc_id` 与入库/溯源引用不受影响 |
