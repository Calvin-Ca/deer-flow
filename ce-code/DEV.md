# 知识库模块 DEV Doc

> **文档定位**：本文回答"知识库如何设计与实现、为什么这样选、检索质量如何度量"。
> 与 PRD 的边界：PRD 定义"要什么、业务上算合格"；本文定义"怎么做、技术上算合格"。
>
> **核心价值在"决策记录"**：配置参数看代码就知道，本文要记录的是"为什么这样选"，让半年后的你或新人不重复踩坑。
>
> **填写说明**：本文以**规范轨**（已落地，Phase B 进行中）为主体；**造价轨**为 Phase C 待办，相关小节标注「Phase C」。
> **进度与待办见 `TODO.md`**（任务编号 T1–T10 与执行批次），本文不重复列。

---

## 1. 整体架构

> 一张图说明数据如何从原始知识源流向可检索状态。
> 知识层 = **数据 + 检索**，不含生成/编排（属任务层 ce-services）。retrieval + rerank 模型只在此加载一份（唯一 owner）。

知识层分**两条并行轨道**，共用同一检索引擎与多表征思路：

| 轨道 | 数据来源 | 核心载体 | 对外原语 | 状态 |
|---|---|---|---|---|
| 规范轨 | PDF（防火/清单/计量规范） | MinerU → 节点树 → Milvus + BM25 | `/search` `/expand` `/clause` | Phase B 进行中 |
| 造价轨 | 定额电子表 + 信息价 + 历史项目 | PostgreSQL（单一事实源）+ KG + Milvus | `/bill/match` `/price/compose` `/quota` | Phase C 待办 |

**规范轨数据流（分阶段流水线，每阶段读上一阶段产物、写自己的产物）：**

```
PDF（建筑规范）
      │
      ▼
[阶段0] MinerU 解析 ──► [阶段1] 切分建树 ──► [阶段2] 挂表征 ──► [阶段3] 索引
  parse.py            build.py(structure)   build.py(reprs)   build.py(index)
  parser/             splitter/(toc)        reprs/(免费4项)   view+retrieval/indexer
      │                    │                                       │
      ▼                    ▼                                       ▼
 data/parsed/        nodes.json(唯一真值)                  BM25 + Milvus + 元数据
 (不可变缓存,只跑1次)  +引用图+祖先链(固有事实)               data/vector_store/{std}/{profile}/
                                                                  │
                                                                  ▼
                                                    ┌──────────────────────┐
                                                    │ retrieval/server.py  │
                                                    │  检索原语（对外契约） │
                                                    │  FastAPI :8100       │
                                                    └──────────────────────┘
```

> **关键设计**：节点树（阶段 1）一次建好，**粒度是索引期（阶段 3）在树上选的视图，不是切树**。换粒度/换表征只重跑下游，不重跑 MinerU（最贵，约 60% 耗时）。结构（建树）/ 表征（多投影）/ 粒度（树上视图）三件事正交分离。

**对外接口契约**（上游模块依赖的唯一边界）：

```
输入（POST /search）：
  query   : str          查询文本
  intent  : str          qa | compliance | cost_match —— 策略选择参数
  k       : int          返回条数（上下文窗消费预算，业务层设定）
  profile : str          parse_profile 名（同一 query 打不同索引做 A/B，可选）
  filter  : standard / version / scope（元数据过滤，可选）

输出：ranked results[]，每条含：
  node_id / clause_path / standard_id / version
  title / content（命中节点正文）
  ancestor_titles（祖先链，溯源用）
  references（引用边）
  provenance（回指 MinerU 原始块：source_file / block_idx / page）
  score（RRF / rerank 相关度）
```

> 上游模块只依赖此契约。下游验收时可用 mock 的"理想检索结果"独立测试上游逻辑。
> **职责边界**：业务层只做三件事——① 选 intent；② 业务对象 → query + filter；③ 设 k。"query 进 → ranked results 出"全包在知识层，检索策略不下放。

---

## 2. 数据处理流水线

### 2.1 解析（Parsing）

- **工具与版本**：MinerU **3.2.0**（本地 venv）/ **3.2.1**（远程 API）；同输入产出 md 逐字一致。配套 mineru-vl-utils **1.0.2**。
- **处理的格式**：PDF（防火/清单/计量规范）。版面/标题/表格/公式抽取。
- **部署方式**：默认走**远程 API**（`172.19.2.2:8000`，常驻热服务，单页 ~1.8s）；`--local` 才本地 CLI。远程 API 每次重传整个 PDF，大 PDF 用分块脚本（80 页/块）。
- ★ **已知问题与 workaround**：
  - **定额/造价类 PDF 必须用 `hybrid-auto-engine`**：密集多列表格，默认 pipeline backend 会列错位。
  - **本地 venv 的 vllm 当前损坏**：`vllm/_C.abi3.so: undefined symbol`，编译时链接的 libtorch 与 PyTorch 2.5.1+cu121 ABI 不匹配；`hybrid-auto-engine` 的 VLM 部分依赖 vllm-async-engine，vllm 一坏整个 hybrid fail。**修复前走远程 API（默认）**；修复方法是 `uv add` pin vllm 到匹配版本，**勿 `uv pip install`**。
  - **mineru_api 输出目录误定位**（已修）：原从历史产物 rglob 取目录，改为从本次 ZIP namelist 取。
  - 表格解析：v1 `table_body` / v2 `content.html` 同为 HTML 串，由 `_HTMLTableParser` + `_expand_spans` 展开 colspan/rowspan 解析成矩形二维表（防串列）。

### 2.2 切分（Chunking）★

> 切分策略直接影响检索效果，是最重要的决策点之一，务必记录依据。
> **本项目语境下"切分"= 建节点树**，而非传统的定长切块——见决策依据。

- **切分策略**：**以 PDF 文档原生目录（TOC）为骨架还原成节点树**（`splitter/toc.py` 的 `TocSplitter`，当前默认/唯一）。目录条目先**物化为骨架节点**（恒存在），正文块再按条文号号段 / 目录归属挂载到骨架下。层级 document → chapter → section → clause → subitem，**深度自适应**（目录列到哪深就到哪）。无目录页时退化为复用 MinerU 标题层级 best-effort。
- **chunk 大小 / overlap**：**不适用固定长度**。粒度是索引期在树上选的视图（`view(tree, level)`），当前仅 `clause` 层已实现；small-to-big 检索期靠 `parent_id` 上探回补整条/整节上下文。
- **决策依据**：
  ```
  决策：以原生目录为骨架建树，而非按字符定长切分
  对比方案：① 按字符切分 ② 按字重启发式判层级 ③ 按原生目录建树（选定）
  结果：建筑规范自带清晰目录，目录是文档的"结构真值"——目录骨架法比
        字符切分/字重启发更可靠，且深度自适应；条款层级/交叉引用一旦按
        字符切分即被破坏，而"适用性判断"依赖完整层级与引用图
  代价：强依赖目录解析质量（方案5 混合定位的阈值需在真规范上微调）；
        无目录页时退化为 MinerU 标题层级 best-effort
  底线：建筑规范首选 toc；其他切法（造价定额表格 → 未来 table_rows，
        Phase C）服务于"跨文档类适配"，不在规范上 second-guess 目录
  ```
- **可插拔设计**：切分做成注册表（`splitter/base.py` 的 `Splitter` 基类 + `splitter/__init__.py` 的 `REGISTRY`），`parse_profile.structure_strategy` 决定本次切法（缺省 `toc`）。换切法 = 换 splitter = 不同 profile = 隔离索引，可直接 ablation 对比召回。

### 2.3 元数据设计

> 每个节点携带的元数据，直接支撑 PRD 第三/四节定义的业务规则。
> 完整 schema 见 `core/schema.py` 的 `Node`（PRD §3.1）。

| 字段 | 类型 | 用途 | 是否支撑业务规则 |
|---|---|---|---|
| `node_id` | string | 稳定 id：条文号有则用（`GB50016#5.3.4`），无则标题路径 | 去重键 / 引用图锚点 |
| `standard_id` | string | 规范号（`GB 50016-2014(2018)`） | 支撑多规范召回 |
| `version` / `effective_date` / `status` | string | 节点级版本/效力（active/superseded/abolished） | 支撑版本冲突、废止条款过滤 |
| `clause_path` | string | 条文号路径（`5.3.4`） | 支撑溯源、`/clause` 直取 |
| `parent_id` / `children_ids` | string / list | 树形结构 | **粒度视图 + small-to-big 全靠它** |
| `ancestor_titles` / `ancestor_paths` | list | 祖先链（建树时一次算定） | 支撑溯源、context_aug 拼接 |
| `references` / `referenced_by` | list | 引用边分型（strong/weak/exclude/cross_standard）+ 反向边 | **引用图扩展核心**（GraphRAG 底座） |
| `provenance` | dict | 回指 MinerU 原始块（source_file / block_idx / page） | **可溯源底线（核心设计原则 2）** |
| `path_source` / `path_confidence` | string / float | 路径来源审计（number/text_level/inherited/synthesized） | 低置信进抽查 |
| `reprs` | dict | 多表征投影（见 2.4 + §4） | 多通道召回 |

> **可溯源是底线**：任何节点都必须能回指其 MinerU 原始块（`provenance.block_idx → data/parsed/` 不可变缓存）。原始内容只读、不可变，派生物只持轻量指针。**不得因任何检索/表征优化被牺牲。**

### 2.4 向量化（Embedding）

- **模型与版本**：bge-large-zh-v1.5（规范轨）/ BGE-M3（造价轨，Phase C，造价术语分布不同不复用）。
- **维度**：dim=1024，max_len=512。
- **部署位置**：服务器 `http://localhost:8097`，model_id `/model`，OpenAI 兼容接口。
- **向量归属约定**：表征层（`reprs/`）只产**待嵌入文本**（`dense` = title+content，`context_aug` = 祖先链‖正文）；**向量由索引期 `retrieval/indexer` 用 embedding 模型统一算**——模型唯一 owner 在检索栈，表征层不加载模型（故仍属"免费"表征）。
- ★ **选型理由**：
  ```
  规范轨用 bge-large-zh-v1.5：中文建筑规范语料，bge 中文系列召回稳定，
  1024 维与检索延迟可接受
  造价轨用 BGE-M3（Phase C）：造价术语（清单/定额/工料机）与规范术语
  分布不同，且 BGE-M3 原生 dense+sparse 混检适配清单匹配；
  是否与规范轨合并为单服务待评估（见 §8 待评估项）
  ```

---

## 3. 存储设计

### 3.1 向量库

- **选型**：Milvus（`http://localhost:19530`，MilvusClient API）。
- **client 版本**：pymilvus **3.0.0**（MilvusClient API；ORM-style 已弃用）。
- **Collection / 索引结构**：按 `{standard}/{profile}` 隔离，collection 名由 `config.collection_name(store_dir.name)` 推断（`07_eval`/`server`/`indexer` 共享同一推断，零改动对齐）。⚠️ 多规范并存时 profile 名须含规范区分，避免 collection 相撞。
- **输出字段**（`engine.MILVUS_OUTPUT_FIELDS`）：含 `node_id` / `parent_id`（small-to-big 锚点）/ `granularity` / `references_to` 等。**已删 `is_mandatory` 字段**（强条机制移除，T3/T4）。
- ★ **索引结构选型**：
  ```
  决策：节点级版本/效力字段（status/version）+ node_id 建 INVERTED 索引
  依据：元数据过滤优先于向量排序（先按 standard/version/scope filter 再 rank），
        故过滤字段需可高效命中；node_id 作去重键与直取锚点亦需 INVERTED
  备注：向量索引类型（HNSW vs IVF）按数据量与延迟权衡，规模尚小，
        当前规范单库约百~千条量级（GB 50016 ~911 条），延迟非瓶颈
  ```

### 3.2 关键词索引

- **方案**：BM25（rank-bm25 库）。
- **语料来源**：`reprs.sparse` 表征 = clause_path + title + content 词项拼接。
- **用途**：补充向量检索的**精确匹配能力**——条文号（"5.3.4"）/ 专业术语精确召回，这是纯向量召回的短板。

### 3.3 关系/图谱存储

- **引用图（规范轨，已实现）**：不另起图库，引用边作为**节点固有事实**落 `nodes.json`（`references` 分型 + `referenced_by` 反向边，建树期 `splitter/references.py` 一次算定）。检索期沿 strong 边强制扩展。**引用图即规范的知识图谱、GraphRAG 底座。**
- **关系库 + KG（造价轨，Phase C 待办）**：
  - 关系库 PostgreSQL = 单一事实来源（`bill_spec` / `quota_item` / `quota_resource` / `resource` / `resource_price` / `hist_bill`，强制 version + region）。
  - KG **P0 用 PG 关联表模拟**（`component_bill_map` MAPS_TO / `bill_quota_map` APPLIES / `quota_resource_detail` CONSUMES），数据量小时 PG join 够用，**P1 再迁 Neo4j** 评估多跳遍历性能。
  - 造价向量库：新建 `bill_spec_kb` collection（BGE-M3 dense+sparse），复用同一 Milvus 实例。

---

## 4. 检索策略 ★

> 实现 PRD §3.4 定义的业务行为。`retrieval/engine.py` 实现混合检索，`retrieval/config.py` 管权重与超参。

### 4.1 召回方式

- **检索模式**：四通道混合检索 + 引用扩展 + rerank。
  ```
  query
    → BM25（条文号/术语精确匹配）
    → 向量（bge-large-zh-v1.5 语义召回）
    → 元数据过滤（standard/version/scope，先圈范围再排序）
    → [modal 通道，按 intent 可选]
    → RRF 合并去重（按 node_id）
    → 引用图扩展（strong 边强制拉取；weak 可选；exclude 禁止扩展）
    → rerank（cross-encoder 精排）
    → small-to-big 回补（命中块 + 完整条/节上下文 + ancestor_titles）
  ```
- **通道权重按 intent 调**（`/search` 接收 `intent` 参数）：

  | intent | 场景 | 主通道 | 召回-精度取舍 |
  |---|---|---|---|
  | `qa` | 规范问答 | 向量 + BM25 均衡，引用扩展开启 | 高召回 + 高精度（控噪） |
  | `compliance` | 图纸审核 | 元数据/scope 过滤优先 | 召回 >> 精度，穷尽适用条款 |
  | `cost_match` | 算量组价（Phase C） | KG 收窄 + BGE-M3 混检 | 候选集高命中，LLM 候选内择优 |

- **权重设计依据**：
  ```
  调优重心在引用图与 condition 通道，向量是"贡献者、非承重墙"。
  原因：规范侧"适用性判断是一切"，引用图与适用范围索引的权重
        高于向量索引（PRD 设计铁律）。
  向量召回在融合中占多大权重属"检索策略"（按 intent），
  而向量库的构建属"知识表示"（离线调优、全业务共享，不按消费者重建）。
  ```

### 4.2 业务规则的技术实现

> 逐条对应 PRD §3.4 的检索硬性约束，说明技术上如何实现。

| PRD 业务规则 | 技术实现 |
|---|---|
| 引用扩展默认开启（strong 边必拉） | 命中节点的 `references` 中 `type=strong/cross_standard` 边无条件拉取（`engine.expand_references`），weak 可选，exclude 禁止正向扩展 |
| 元数据过滤优先于向量排序 | 先按 `standard/version/scope` filter 圈定范围，再在范围内 RRF/rerank（`condition` 过滤先于排序） |
| 无全局强条置顶 | **强条机制整套移除**（T3）：所有结果按 RRF/rerank 排序后切 top_k；`modal`（语气：应/宜/可/严禁）降级为可选召回通道，query 带强制意图时才 filter/并入 sparse，**不参与全局重排** |
| 跨规范召回 | `references` 中 `type=cross_standard` 边触发多规范召回；跨规范引用查不到 meta 时自动跳过 |
| 版本/废止 | 节点级 `status`/`version`/`effective_date`；废止节点不参与召回但保留（可回答"何时废止"） |
| small-to-big 上探 | 细粒度命中后靠 `parent_id` 上探返回整条/整节（**去重键已切 node_id ✅，上探回补待做** — T9） |
| 无结果不能编 | 向量未命中 → BM25 兜底；仍无 → 返回父级章节而非杜撰（PRD_v1 §验收"无结果不能编"） |

---

## 5. 检索质量度量 ★

> ⚠️ 边界说明：本节是**技术验收**，与 PRD 的业务验收解耦。
> 这些指标在**检索接口处闭环**，不依赖任何下游模块表现。**端到端任务指标归业务层。**

### 5.1 评估方法

- **评估集**：`data/eval_set/`（入 git）。当前 GB 50016 评测集 `gb50016_eval.json`（45 条用例）。单条格式：
  ```json
  {
    "query": "24m 高的住宅楼疏散楼梯最小宽度是多少？",
    "expected_clauses": ["GB 50016-2014(2018) 5.5.30", "5.5.31"],
    "intent": "qa",
    "user_type": "通用咨询"
  }
  ```
  金标（`expected_clauses`）由业务层参与定义（"做成我的任务，你必须捞出这些"）。
- **评估工具**：自建脚本 `python -m tools.eval`（原 `07_eval.py` 去数字前缀）。`--store-dir data/vector_store/<std>/<profile>`，每个 profile/intent 跑同一评测集，指标记一张表对比。**一次只动一个变量。**
- **判命中口径**：⚠️ **按包含关系判命中**——返回块**包含或等于**目标条即算命中（非严格 `node_id` 相等），否则只到结构/粗粒度的 profile 会被系统性低估，ablation 结论失真。clause 粒度下现有精确集合判命中与包含关系等价。

### 5.2 指标与目标

| 指标 | 含义 | 目标 | 备注 |
|---|---|---|---|
| Recall@k | 正确条文出现在前 k 的比例 | 首要指标 | k 取业务约定值；规范/审图/造价轨均适用 |
| 引用条款召回率 | 被引用的关联条款是否被拉取 | — | 检验引用图扩展 |
| MRR / 金标秩 | 排序敏感度 | — | 避免"金标排第 95 位也算召回" |
| 适用性误判率 | condition 谓词匹配准确性 | — | Phase B 谓词数据后启用 |
| 检索延迟 (P95) | 不含下游生成 | 待定 | 规模尚小非瓶颈 |
| 平均返回 token 数 | 成本观测 | — | 配合 precision/噪声看 |

**造价侧（Phase C）**：清单候选集命中率、定额套用准确率（KG 关系正确性）须支撑业务层端到端红线（编码 Top-1 ≥ 85%）。

> ⚠️ **当前护栏现状（2026-06-13）**：流水线后半段一度与 `nodes.json` 脱钩，`07_eval` 打的是 T2 之前旧 `*_clauses.json` 建的陈旧索引。**新树端到端可建索引、可被 eval 与 v1 基线对比** 是当前唯一阻塞里程碑（见 §7）。此步打通前"每步过护栏"的纪律无所附丽。

---

## 8. 依赖服务清单

> 只列依赖的**外部服务/设施**（数据库、模型、解析服务）及地址；Python 包不在此列。

### 规范轨（已部署）

| 角色 | 服务/模型 | 地址 | 用途 |
|---|---|---|---|
| Embedding | bge-large-zh-v1.5 | `http://localhost:8097` | 条款向量化、query embedding（dim=1024，max_len=512） |
| 向量库 | Milvus | `http://localhost:19530` | 向量存储与检索 |
| VLM | Qwen2.5-VL-7B | `http://localhost:8098` | PDF 解析时图示理解 |
| 文本推理 | Qwen3-8B | `http://localhost:8099` | 查询改写、引用图 LLM 校验（`/no_think` 禁思考链） |
| PDF 解析 | MinerU API | `http://172.19.2.2:8000` | PDF 解析（默认远程，hybrid-auto-engine） |
| 知识服务 | 本模块（FastAPI） | `http://localhost:8100` | 对外检索原语 `/search` `/expand` `/clause` `/health` |

### 造价轨（Phase C 待部署）

| 角色 | 服务/模型 | 地址 | 用途 |
|---|---|---|---|
| 关系库 | PostgreSQL | 待部署 | 规范/定额/价格/历史精确查询（单一事实源） |
| KG（P0） | PG 关联表（P1 迁 Neo4j） | 同上 | 构件→清单→定额→工料机多跳关系 |
| Embedding（造价） | BGE-M3 | 待部署 | dense+sparse 混检；与规范轨是否合并单服务待评估 |
| 向量库 | Milvus（复用实例） | `http://localhost:19530` | 新建 `bill_spec_kb` collection |

---

## 附录：关键决策速查

> 把全文带 ★ 的决策汇总于此，便于快速回顾"为什么当初这么定"。

| 决策点 | 选择 | 一句话依据 |
|---|---|---|
| 切分策略 | 原生目录（TOC）为骨架建节点树（`toc` splitter） | 目录是建筑规范的"结构真值"，比字符切分/字重启发可靠且深度自适应；按字符切分会破坏条款层级与交叉引用 |
| 粒度模型 | 索引期在树上选视图（`view`），非切树 | 三件正交（结构/表征/粒度），换粒度只重跑阶段 3，不重跑最贵的 MinerU |
| embedding 模型 | 规范轨 bge-large-zh-v1.5 / 造价轨 BGE-M3 | 按轨选型（术语分布不同），非按 agent；向量库离线调优、全业务共享 |
| 向量索引归属 | 向量由索引期 `indexer` 统一算，表征层只产待嵌入文本 | embedding 模型唯一 owner 在检索栈，表征层不加载模型 |
| 引用图存储 | 落 `nodes.json` 作固有事实，不另起图库 | 引用图即规范知识图谱/GraphRAG 底座，建树期一次算定 |
| 强条排序 | **整套移除**，语气降级为 `modal` 可选通道 | 无全局强条置顶，结果纯按 RRF/rerank 排；语气无法律含义 |
| hybrid 权重 | 按 intent 调，引用图/condition 权重 > 向量 | 规范侧"适用性判断是一切"，向量是贡献者非承重墙 |
| 判命中口径 | 按包含关系（非严格 node_id 相等） | 否则粗粒度 profile 被系统性低估，ablation 失真 |
| 可溯源 | 每节点 `provenance` 回指 MinerU 原始块，不可牺牲 | 改算法重派生/人工核对/PDF 高亮/出处可查全靠它（核心设计原则 2） |
