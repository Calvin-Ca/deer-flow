# 组价知识库 DEV Doc

> **定位**：本文回答"组价知识库如何设计与实现、为什么这样选、质量如何度量"。PRD 定义"要什么、算不算合格"；
> 本文定义"怎么做"。**核心价值在决策记录**（为什么这样选），配置参数看代码即知。进度见 `TODO.md`。
>
> **范围（2026-06-18 重构后）**：ce-code = **深圳房建组价知识库**。规范条文检索 RAG 已移除（防火轨停做）。
> 两条硬约束（PRD §5 落到技术）：
> - **地区强隔离**：只收录/只取深圳本地现行有效标准（深圳有独立 2024 版消耗量标准，与省内他市口径不同）。
> - **版本严格隔离**：同号国标不同年版（GB/T 50854 的 2013/2024）同 9 位码不同义 → 按 `spec` 路由，
>   `bill_spec` 复合主键 `(code, spec_version)`、`bill_quota_map` 带 `bill_spec_version`、向量库分 collection（见 §3）。

---

## 1. 整体架构

```
PDF（清单/计量规范/定额/信息价/费率）
   │  [摄取 ingest]
   ▼
MinerU 解析 ──► toc 切分建树 ──► chunks.json（单一真值，含表格矩形化 + 引用图/祖先链）
 -m ingest.parser   -m ingest.splitter        data/structured/<规范>/default/
   │  [抽取 cost]
   ▼
cost/ 各模块（chunks.json → 结构化 jsonl）          [入库] load_pg --scan-dir
 bill_spec / quota / price / fee_rate /        ──►  PostgreSQL ce_cost（单一事实源）
 price_composition / bill_quota / resource_*         治理字段 doc_id/spec_version/region/effective_priority
   │  [取数/召回]
   ├──► cost.bill_index：PG bill_spec → Milvus 清单向量库（按 spec 分 collection）
   ▼
service/cost_api（FastAPI :8100）对外组价取数原语：
   /bill/match     构件描述 → 清单候选（dense 召回 + 结构约束/现浇预制重排；spec 必填路由）
   /price/compose  清单 → 定额 → 工料机含量 ⋈ 信息价（spec 版本过滤；未命中价 no_source 不杜撰）
   /quota          定额子目直取（子目 + 人材机含量）
```

- **摄取（ingest/）与抽取（cost/）解耦**：MinerU 解析最贵（约 60% 耗时）落 `data/parsed/` 不可变缓存，只跑一次；
  换抽取规则只重跑 cost，不重跑解析。
- **清单 / 定额 / 信息价三者解耦**：分表存储、独立抽取管道、三条独立版本轴（清单按国标年版 / 定额按 SJG 版本 /
  信息价按月度时效），靠映射表 `bill_quota_map`（清单→定额）、`resource_price_map`（定额资源→信息价物料）连接，
  `compose_price` 读时按需 join。换国标版本只动清单轴、信息价月更只动价轴。

---

## 2. 摄取流水线（PDF → chunks）

- **解析**：`ingest/parser/`（MinerU），走远程 API（`172.19.2.2:8000`，`hybrid-auto-engine` 后端——定额/造价含密集
  表格，逐列对位防错位）。产 `_content_list.json`（分块流，带 type/text_level/page/bbox）。表体由 `FormatAdapter`
  从 `table_body` HTML 经 `_HTMLTableParser`+`_expand_spans` 矩形化（展开 colspan/rowspan 防串列）。
- **切分**：`ingest/splitter/`（toc）。基于原生目录建节点树（chunks.json，单一真值），保住「条→款」从属 + 交叉引用 +
  祖先链。`-m ingest.splitter toc --input <content_list> --subsplit number` 一步出 chunks。
- **IR 契约**：`ingest/ir/` 的 `@dataclass`——`Document/Block`（解析）、`Chunk/Reference/Provenance`（切分·单一真值）、
  `ParseProfile`（流水线配置）。

> 切分质量对定额/SJG 偏弱（SJG 无规整目录，chapter/ancestor_titles 可能弱）→ cost 抽取用**单位格锚定 / 表头判别**
> 等不依赖完美树形的策略（见 §3 各抽取器），入库后人工抽查。

---

## 3. 存储设计

### 3.1 关系库 PostgreSQL（组价单一事实源）

服务器容器 `ce-postgres`（端口 `5433`，库 `ce_cost`，用户 `cost`；rootless docker，data-root 落 nvme，见 §6）。
DDL `cost/schema.sql`（`IF NOT EXISTS` 幂等），导入 `cost/load_pg.py`（按主键 `ON CONFLICT` 幂等 upsert）。

| 表 | 内容 | 主键 / 唯一键 | 抽取器 |
|---|---|---|---|
| `bill_spec` | 清单项规范（编码/名称/特征/单位/计算规则/工作内容） | **`(code, spec_version)`** | `bill_spec.py` |
| `aux_table` | 辅助/参数表（土石分类、工作面宽度…原样矩形 body） | `(doc_id, caption, chapter)` | `bill_spec.py` |
| `quota_item` / `resource` / `quota_resource` | 定额子目 / 人材机 / 子目×资源含量 | `(region,quota_code,spec_version)` / `(category,name,spec,unit)` / `(quota_id,resource_id)` | `quota.py` |
| `resource_price` | 信息价（月度价 + 时效区间，`btree_gist` EXCLUDE 防重叠） | resource_id+region+期 先删后插 | `price.py` |
| `fee_rate` | 费率（安文/总包/增值税/附加税…参考范围 + 推荐值） | `(doc_id,fee_category,fee_name,applicable)` | `fee_rate.py` |
| `price_composition` | 费用构成规则（综合单价 6 项 / 工程造价 4 部分） | `(doc_id,composite,seq)` | `price_composition.py` |
| `bill_quota_map` | 清单→定额 APPLIES（带 confidence/source） | **`(bill_code, bill_spec_version, quota_code, quota_doc_id)`** | `bill_quota.py` |
| `resource_price_map` | 定额资源↔信息价物料 同物异名对齐（unit_factor） | `(quota_resource_id, price_resource_id)` | `resource_price_map.py` |

**治理字段**（每表强制）：`doc_id` / `spec_version` / `region`（深圳=本地）/ `effective_priority`（本地=1 最高）。
价格 `resource_price` 带 `effective_period` 时效，不参与 effective_priority。

**版本严格隔离**（`config.SPEC_REGISTRY` + `resolve_spec`）：`spec` ∈ {2013, 2024} → 路由 bill_collection +
bill_spec_versions 过滤集 + supports_compose 标志。`bill_spec` 复合主键让同 9 位码跨年版共存；`compose_price` /
`bill_quota_map` 按 spec_version 过滤，杜绝跨版本串库。2024 supports_compose=True（有定额/价格/映射），2013=False
（组价数据未就绪，仅清单匹配可用，`/price/compose?spec=2013`→501）。

### 3.2 清单向量库（Milvus，供 /bill/match）

源 = PG `bill_spec`（造价取数一律走 PG）。`cost/bill_index.py` 把每条清单项嵌成向量入 Milvus。
- **嵌入**：复用 bge-large-zh-v1.5 @ :8097（dim 1024），`cost/embed.py`。嵌入文本 = `清单名。特征(feature_schema)。章节`。
- **schema**：code(INVERTED 直取/过滤) + name/unit/feature/chapter/doc_id/spec_version + **cast_type**(现浇/预制标记，
  从 caption 派生) + embedding。
- **按 spec 分 collection**：`cost_bill_spec_kb`（2024）/ `cost_bill_spec_kb_2013`。`bill_index --spec 2024/2013`
  按 SPEC_REGISTRY 自动取 collection + doc_ids（防漏写 --doc-id 混版本）。

---

## 4. 取数 / 召回策略

### 4.1 /bill/match —— 构件 → 清单候选（召回，知识层只给候选）

`cost/bill_match.search_bill`：embedding(query) → Milvus COSINE 召回候选池 → **结构约束稳定重排** → 截 top_k。
- **结构约束（确定性，纯函数）**：① 类型对齐（`STRUCTURAL_MARKERS` 模板/钢筋/脚手架…，查询没提及则下压附属/措施项）；
  ② 现浇/预制（`cast_type`=预制而查询未提"预制"→下压，房建默认现浇）。对本体零扰动。
- **专业域过滤**：`code_prefixes`（如 01 建筑/03 安装）剔跨专业噪声。
- cross-encoder rerank 已移除（2026-06-17 实测在「构件描述 × 极短清单名」上劣化 dense，E4）。
- **职责**：知识层只召回候选；选码（Top-1）归任务层 LLM 在候选内做（红线：只建议不定稿）。

### 4.2 /price/compose —— 组价取数链

`cost/query.compose_price`：bill_spec(spec 过滤) → bill_quota_map(APPLIES, 带 confidence + bill_spec_version 过滤) →
quota_item → quota_resource → resource ⋈ resource_price（信息价两路取价：直连优先 / 经 resource_price_map 套
unit_factor；按 region + 时效取，on_date 命中期优先、缺省取最新可用期）。
- **红线**：未命中信息价的工料机 `unit_price=None` + `price_status="no_source"`，**绝不杜撰**，amount 仅在有价时算；
  缺口交任务层 HITL 询价 / web_search。

---

## 5. 质量度量

- **评测对象**：`/bill/match` 召回质量。`tools/eval_bill.py` 读 `match_gold.jsonl`（构件描述→编码）→ 召回 →
  按**编码精确相等**判命中 → Top-1 / Top-3 / Recall@k / MRR / 平均金标秩（排序敏感）。
- **gold**：`tools/build_match_gold.py` 从真实结算 xlsx 转（query=COMP_NAME+FEATURE 不含清单名避循环；gold=前 9 位码；
  覆盖过滤 + 脱敏）。
- **红线（PRD §6）**：Top-1 ≥ 85% / Top-3 ≥ 95%。**归属**：知识层对召回（Recall@k）负责，Top-1 选码归任务层 LLM
  （它拿 top-k 候选）。实测真实 gold Recall@10 偏低且 miss 多在库 → 召回是当前瓶颈（提升项见 notebooks/BACKLOG）。
- **实验记录体系**：`notebooks/`（每次实验 dated 文件夹 + experiments.md 结论时间线 + BACKLOG 待办），见 `notebooks/README.md`。

---

## 6. 依赖服务

| 服务 | 地址 | 用途 |
|---|---|---|
| PostgreSQL `ce-postgres` | `localhost:5433`（库 ce_cost / 用户 cost） | 组价单一事实源 |
| Milvus | `172.19.3.136:19530` | 清单向量库（/bill/match） |
| Embedding bge-large-zh-v1.5 | `172.19.3.136:8097`（dim 1024） | 清单嵌入（建库 + 召回） |
| MinerU API | `172.19.2.2:8000`（hybrid-auto-engine） | PDF 解析（摄取层，远程热服务） |

- **PG 部署**：服务器系统共用 docker 与满盘 `/` 不可动 → 用 **rootless docker** 起独立 daemon（caic 用户，
  data-root=`/mnt/nvme/calvin/docker/data`），`postgres:16` 容器，卷落 nvme。连 `ce-postgres` 须走 rootless
  （`docker context use rootless` 或设 `DOCKER_HOST`），**绝不用 `sudo docker`**。
- `psycopg`：服务器 `uv add 'psycopg[binary]'`（勿 `uv pip install`）。连接串密码经 `CE_PG_DSN` / `~/.pgpass` 传，
  不写进仓库。
- 环境：Python 3.12 / uv 0.11；依赖统一 `uv add`。本地 Mac 装不了 torch(cu121) → 本地只 py_compile + 纯函数单测，
  Milvus/PG/嵌入真链路在服务器跑（见仓库根 CLAUDE.md）。

---

## 附录 · 关键决策速查

- **造价取数一律走 PG**（不走 chunks.json）：chunks 只是抽取的中间产物，单一事实源是 PG。
- **清单/定额/信息价解耦**：三表 + 两映射表，compose 读时 join；换版本/月更互不牵动。
- **版本隔离三层**：bill_spec 复合主键 + bill_quota_map 版本维度 + collection 分库；spec 必填无默认（逼调用方选版本）。
- **现浇优先**：房建 BIM 构件默认现浇，预制需查询明示（cast_type down-rank）。
- **嵌入复用规范轨 bge-large 单 dense 通道**（不新部署 BGE-M3）；sparse 混检为召回提升储备项（见 BACKLOG）。
- **2013 用 GB-50854-2013（房建计量规范）**为清单源，非 GB-50500-2013（计价规范，措施编码体系不同）。
