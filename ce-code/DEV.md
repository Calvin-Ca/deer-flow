# ce-code（知识层）· 开发指南

> 知识层的**实现策略与技术路径**。需求见 `PRD.md`，进度见 `TODO.md`，操作命令（流水线/起服务）见 `README.md`，项目级共享约定见根 `CLAUDE.md`。

---

## 实现架构

知识层分**两条并行轨道**，共用同一检索引擎与多表征思路：

| 轨道 | 数据来源 | 核心载体 | 对外原语 |
|---|---|---|---|
| 规范轨 | PDF（防火/清单/计量规范） | MinerU → 条款树 → Milvus + BM25 | `/search` `/expand` `/clause` |
| 造价轨 | 定额电子表 + 信息价 + 历史项目 | PostgreSQL（单一事实来源）+ KG + Milvus | `/bill/match` `/price/compose` `/quota` |

规范轨 Phase B 进行中；造价轨 Phase C 待办，两轨解耦可并行。

---

## 规范轨实现

### 解析管线（5 个阶段）

管线每阶段读上一阶段产物、写自己的产物；`parse_profile` 控制终止阶段，实验只重跑下游，不重跑 MinerU（最贵，约 60% 耗时）。

| 阶段 | 脚本/模块 | 产物路径 |
|---|---|---|
| 0 MinerU 解析 | `pipeline/01_parse_pdf.py` + `pipeline/mineru_api.py` | `data/parsed/{standard}/` |
| 1+2 结构/粒度轴 | `pipeline/02_extract_clauses.py` | `data/structured/{std}/{profile}/clauses.json` |
| 3 增强轴 | `extract/build.py`（编排富化链） | 同上（附 `references`/`ancestor_titles`/`is_mandatory_clause` 等增强字段） |
| 4 索引 | `pipeline/04_build_index.py` | `data/vector_store/{std}/{profile}/` |
| 质量审核 | `pipeline/03_review_quality.py` | 人工检查，不阻塞流水线 |

**MinerU 使用原则：**
- 默认走远程 API（`172.19.2.2:8000`，常驻热服务，单页 ~1.8s）；`--local` 才本地 CLI
- **定额/造价类 PDF 必须用 `hybrid-auto-engine`**（密集多列表格，pipeline backend 列错位）
- 远程 API 每次重传整个 PDF；大 PDF 用 `split_and_parse.py` 分块（80 页/块）

**parse_profile 实验隔离（命名避免覆盖彼此结果）：**

```yaml
parse_profile:
  name: p2_clause_full
  terminal_stage: enrich       # structure | granularity | enrich | index
  chunk_granularity: natural   # node | paragraph | natural
  enrichment: full             # none | ids_refs | full
  small_to_big: true
```

产物路径和 Milvus collection 按 `{standard}/{profile}` 隔离；`/search` 接 `profile` 参数，同一 query 打不同索引做 A/B 对比。

### 构建层（extract/）

`extract/build.py` 是富化链编排器：把 v1 条款（`02` 输出）→ v2 schema（含可选增强字段）。

```
read_clauses(v1_json)
  → extract/references.py    引用边分型 + 双向（strong/weak/exclude/cross_standard + referenced_by）
  → extract/strength.py      modal_strength（语气词）/ is_mandatory_clause（黑体；官方清单优先→MinerU字重→保守False）
  → extract/ancestors.py     ancestor_titles（章/节标题链，向量化时拼入 small-to-big 上下文）
  → extract/scope.py         applicable_scope 谓词（当前统一填 unknown，Phase B 待实现）
  → schema.to_v1_compat()    兼容桥（重建索引前 retrieval/engine.py 不崩）
```

**增强字段均可空**：`has_mandatory_marking: False` 的规范（如造价规范），`is_mandatory_clause` 不填、强条机制不激活；`scope.py` 抽不准则 `scope_status: unknown`，走保守召回（宁多召不漏）。

### 检索引擎（retrieval/）

`retrieval/engine.py` 实现四通道混合检索，`retrieval/config.py` 管权重与超参：

```
query
  → BM25（rank-bm25；条文号/术语精确匹配）
  → 向量（bge-large-zh-v1.5，dim=1024，MilvusClient API）
  → 元数据过滤（standard/version/scope，先圈范围再排序）
  → RRF 合并去重
  → 引用图扩展（strong 边强制拉取；weak 可选；exclude 边禁止扩展）
  → rerank（cross-encoder 精排）
  → small-to-big 回补（返回命中块 + 完整条/节上下文 + ancestor_titles）
```

**通道权重按 intent 调**（`/search` 接收 `intent` 参数）：

| intent | 主通道 | 说明 |
|---|---|---|
| `qa` | 向量 + BM25 均衡，引用扩展开启 | 高召回 + 高精度 |
| `compliance` | 元数据/scope 过滤优先 | 穷尽适用条款，召回 >> 精度 |
| `cost_match` | KG 收窄 + BGE-M3 混检（Phase C） | 候选集高命中，LLM 候选内择优 |

**检索硬性约束**：引用扩展默认开启；元数据过滤优先于向量排序（先 filter 再 rank）。

### 服务层（service/）

`service/server.py`：FastAPI，监听 `:8100`，只暴露检索原语，不含生成。

```
POST /search          裸检索（intent / k / profile 参数）
POST /expand          对 node_id 做引用扩展
GET  /clause/{std}/{path}   单节点直取
GET  /health          含 ready_standards / vector_store / deps 地址
```

待补（依赖 Phase B 谓词数据）：`/filter`（适用范围过滤）、`/rerank`。

---

## 造价轨实现（Phase C）

关系库是单一事实来源，KG 由其派生，向量库为语义补充。**实现顺序：关系库建表 → 数据入库 → 跑通取数路径 → 加向量召回 → 加 KG 多跳**。

### ① 关系库 PostgreSQL

建表（DDL 详见 `cost_agent_tech.md`）：

| 表 | 职责 | 关键约束 |
|---|---|---|
| `bill_spec` | 清单规范条目（9 位统一编码 + calc_rule + feature_schema） | `spec_version` 版本管理 |
| `quota_item` | 定额子目（人材机费 + 基价） | `region` + `version` 强制 |
| `quota_resource` | 定额→资源含量（consumption） | 外键 `quota_item` |
| `resource` / `resource_price` | 资源及价格（带 `effective_period` 时效） | 按 region + 时效查 |
| `hist_bill` | 历史工程清单（脱敏 + 质量标注） | 供历史相似案例召回 |

**数据入库方式**：
- GB 50500/50854：复用管线 02 产物（条款树 → 规则抽取 `bill_spec` 字段入库）
- 定额电子表：CSV 清洗 → `quota_item` + `quota_resource` + `resource`
- 信息价：定期抓取/导入 `resource_price`，带 `effective_period`

### ② 知识图谱（P0 用 PG 关联表模拟）

建三张关联表替代 Neo4j（数据量小时 PG join 够用，P1 再迁）：

```sql
component_bill_map   (component_type, bill_code)               -- MAPS_TO
bill_quota_map       (bill_code, quota_code, region)            -- APPLIES
quota_resource_detail (quota_code, resource_id, consumption)   -- CONSUMES
```

跑通组价取数路径后，再评估是否需要 Neo4j 多跳遍历性能。

### ③ 向量库（Milvus bill_spec_kb）

- BGE-M3 dense+sparse 混检（不复用规范轨 bge-large-zh-v1.5，造价术语分布不同）
- 新建 `bill_spec_kb` collection，复用已有 Milvus 实例
- 向量化内容：清单条目名称 + 特征描述 + 工程做法说明

**两条检索路径**：
```
① 清单匹配：BGE-M3 混合召回 top-K → KG MAPS_TO 关系收窄 → LLM 候选内择优（选码+置信度）
② 组价取数：KG APPLIES 取定额 → CONSUMES 取工料机含量 → resource_price 按 region+时效取价
```

---

## 依赖服务

### 规范轨（已部署）

| 角色 | 模型/服务 | 地址 | 用途 |
|---|---|---|---|
| Embedding | bge-large-zh-v1.5 | `http://localhost:8097`，model_id `/model` | 条款向量化、query embedding（dim=1024，max_len=512） |
| 向量库 | Milvus | `http://localhost:19530` | 向量存储与检索（MilvusClient API） |
| VLM | Qwen2.5-VL-7B | `http://localhost:8098`，model_id `/model` | PDF 解析时图示理解 |
| 文本推理 | Qwen3-8B | `http://localhost:8099`，model_id `qwen3-8b` | 查询改写（生成 3-5 变体）、引用图 LLM 校验 |
| MinerU API | MinerU 3.2.1 | `http://172.19.2.2:8000` | PDF 解析（默认远程，hybrid-auto-engine 可用） |

> Qwen3-8B：`/no_think` 后缀禁用思考链（JSON 输出必用，避免输出污染）。
> 生成/问答/合规编排不在知识层——那是任务层（`../ce-services/`）的事。

### 造价轨（待部署）

| 角色 | 选型 | 地址 | 约束 |
|---|---|---|---|
| 关系库 | PostgreSQL | 待部署 | JSONB 存 feature_schema；所有表强制 version + region |
| KG（P0） | PG 关联表（P1 迁 Neo4j） | 同上 | 多跳遍历数据量小时 PG join 已够 |
| Embedding（造价） | BGE-M3 | 待部署 | dense+sparse 混检；是否与规范轨合并为单服务待评估 |
| 向量库 | Milvus | `http://localhost:19530` | 复用同一实例，新建 `bill_spec_kb` collection |

> 算量引擎（几何+扣减）、图纸解析（IFC/DXF）、MinIO 属**任务层**，不在知识层依赖范围。

**依赖健康自检（排查"起不来"先逐个确认依赖活着，命令单行）：**

```
curl -s http://localhost:8097/v1/models
curl -s http://localhost:8098/v1/models
curl -s http://localhost:8099/v1/models
curl -s http://localhost:9091/healthz
curl -s http://localhost:8100/health
```

---

## 开发环境

- **GPU 选择**：MinerU 解析用 `CUDA_VISIBLE_DEVICES=2`（GPU 2 空闲显存最多 ~17 GB；GPU 1/3 被 vLLM 占用）
- **模型下载**：`HF_ENDPOINT=https://hf-mirror.com`（服务器默认无法直连 HuggingFace）
- **关键依赖版本**：
  - MinerU **3.2.0**（本地 venv）/ **3.2.1**（远程 API）；同输入产出 md 逐字一致
  - mineru-vl-utils **1.0.2**，`transformers>=4.51.1,<5.0.0`（**不可升 5.x**，Qwen2VLConfig 不兼容）
  - pymilvus **3.0.0**（MilvusClient API；ORM-style 已弃用）；rank-bm25 ✓
  - PyTorch **2.5.1+cu121**（`[tool.uv] override-dependencies` 绕过 mineru 的 >=2.6.0 声明）

> 共享环境基础（服务器路径、Python 版本、uv 版本、GPU 硬件）见根 `CLAUDE.md` §2.3。

### ⚠️ venv 的 vllm 当前损坏（本地 CLI hybrid 前置阻塞）

```
vllm/_C.abi3.so: undefined symbol: _ZN3c106ivalue14ConstantString6create...
```

vllm 编译时链接的 libtorch 与 PyTorch 2.5.1+cu121 ABI 不匹配，`hybrid-auto-engine` 的 VLM 部分依赖 `vllm-async-engine`，vllm 一坏整个 hybrid fail。**修复前走远程 API（默认）；修复方法：`uv add` pin vllm 到与 torch 2.5.1 匹配的版本，勿 `uv pip install`。**
