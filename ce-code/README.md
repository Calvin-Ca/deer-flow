# ce-code（知识层）

建筑规范 RAG 的**知识层（数据 + 检索）**。本文件只**涉及**：目录结构、流水线命令、起服务。

> - 需求/设计（领域铁律、schema、多表征、检索/造价设计、端点规格）见 `PRD.md`
> - 依赖服务与环境（Embedding/VLM/Milvus/版本约束/GPU）见 `DEV.md`
> - 进度与评测指标见 `TODO.md`
> - 项目级共享上下文（设备分工、git 约定、服务器环境）见仓库根 `CLAUDE.md`
> - **任务层（生成/合规编排）在 `../ce-services/`**，是本服务的纯 HTTP 客户端，文档见 `../ce-services/README.md`

---

## 目录结构

```
ce-code/
├── README.md                       # 本文件（操作手册）
├── PRD.md / DEV.md / TODO.md        # 需求设计 / 开发环境 / 进度
├── pyproject.toml                  # uv 管理依赖
├── .gitignore                      # 忽略 data/ 下大文件与解析产物
├── data/                           # 数据资产（除 eval_set 外均不入 git）
│   ├── raw/                        #   原始 PDF（手动放入）
│   ├── parsed/                     #   MinerU 解析输出（python -m parser 产物，阶段 0 缓存）
│   ├── structured/                 #   Chunk 树 chunks.json（build 结构层产物）
│   ├── vector_store/               #   BM25 + Milvus 索引（build 索引层产物）
│   ├── eval_set/                   #   评测集（入 git）
│   │   └── gb50016_eval.json       #     GB 50016 的 45 条评测用例
│   └── quality_reports/            #   质量审核报告（tools/review_quality 输出）
│
│  ── 编排 / 入口（从 ce-code 根运行）──
├── build.py                        # 构建入口（薄壳，转 service/build_service.main）
├── config.py                       # 共享运行配置：服务地址 / 规范别名 / collection 命名
│
│  ── ir：统一 IR 契约（@dataclass + to_dict/from_dict）──
├── ir/                             # 各阶段中间表示（IR），全层只认这里
│   ├── document.py                 #   Document / Block（解析层产物）
│   ├── chunk.py                    #   Chunk / Reference / Provenance（切分层产物·单一真值）
│   ├── feature.py                  #   ChunkFeature（表征层产物）
│   ├── query.py                    #   RetrievalQuery（检索入参）
│   ├── retrieval.py                #   RetrievedChunk（检索命中·含对外契约 to_response）
│   ├── context.py                  #   KnowledgeContext（一次检索结果集 = /search 返回体）
│   └── profile.py                  #   ParseProfile（流水线配置契约）
│
│  ── ① 解析层（多解析模型可插拔）──
├── parser/                         # 原始文档 → Document IR
│   ├── base.py / factory.py        #   Parser 基类 + 工厂（profile.parser_strategy 选）
│   ├── mineru.py                   #   ★ MinerU 工具（单文件）：门面 MineruParser + 适配 FormatAdapter
│   │                               #     + 阶段0 引擎 parse_via_api + CLI run_command
│   ├── unstructured.py             #   ◌ 占位
│   └── __main__.py                 #   阶段 0 启动脚本（registry 驱动）：python -m parser <工具>
│
│  ── ② 切分层（多切法可插拔）──
├── splitter/                       # Document → Chunk 树
│   ├── base.py / factory.py        #   Splitter 基类 + 工厂（profile.structure_strategy 选）
│   ├── toc_splitter.py             #   ★ TocSplitter：基于原生目录的多层级切分（切分深度 toc_max_depth/subsplit 可控）
│   │                               #     三内部件（目录打标/建树/引用图分型）2026-06-15 已合并入此单文件，按 §1/§2/§3 分段
│   ├── semantic_splitter.py        #   ◌ 占位（语义切）
│   ├── tree_splitter.py            #   ◌ 占位（标题层级树）
│   └── __main__.py                 #   阶段 1 启动脚本（registry 驱动）：python -m splitter <切法>
│
│  ── ③ 表征层（多表征可插拔）──
├── feature/                        # Chunk → 多表征（挂 chunk.features）
│   ├── base.py / pipeline.py       #   Feature 基类 + 注册表/enrich（profile.features 选）
│   ├── raw / bm25 / dense / context_aug.py   #   ★ 免费 4 项（bm25=旧 sparse）
│   └── keyword.py / graph.py       #   ◌ 占位
│
│  ── ④ 索引层（多索引可插拔）──
├── index/                          # Chunk 树 → 选粒度视图 → 各索引
│   ├── manager.py                  #   ★ 粒度视图 view（含空骨架过滤）+ 行准备 + 编排 build_index
│   ├── bm25_index.py               #   ★ rank-bm25 倒排（消费 sparse）
│   ├── vector_index.py             #   ★ Milvus 向量（消费 dense，索引期统一嵌入）
│   ├── metadata_index.py           #   ★ metadata.json 快照（+ clause 直取/引用扩展读取）
│   └── graph_index.py              #   ◌ 占位（面向 Phase C 造价 KG）
│
│  ── ⑤ 检索层（多召回可插拔）──
├── retrieval/                      # RetrievalQuery → RetrievedChunk
│   ├── base.py                     #   Retriever 基类
│   ├── bm25_retriever / dense_retriever.py   #   ★ 单路召回
│   ├── hybrid_retriever.py         #   ★ BM25+向量+RRF+引用扩展+rerank（主力，逐字保持旧召回）
│   ├── rrf.py                      #   ★ RRF 合并 + 引用扩展（行 dict 层纯函数）
│   ├── graph_retriever.py          #   ◌ 占位（KG 多跳）
│   └── service.py                  #   RetrievalService（统一检索入口：search/expand/get_clause）
│
│  ── ⑥ 服务层（对外 API + 构建编排）──
├── service/                        # 承旧 server.py + build.py
│   ├── build_service.py            #   构建编排（阶段 1→3）：解析→切分→表征→索引
│   ├── retrieve_service.py         #   检索编排 + 可观测性（请求级日志/计时）
│   └── knowledge_api.py            #   知识服务 :8100（/search /expand /clause /health，契约不变）
│
│  ── 造价数据轨（Phase C，与 RAG 流水线解耦）──
├── cost/                           # 结构化造价数据 → PostgreSQL（见末节「造价数据轨」）
│   ├── bill_spec.py                #   chunks.json → bill_spec.jsonl + aux_tables.jsonl（双出口抽取）
│   ├── schema.sql                  #   全表 DDL（bill_spec/aux_table/quota_*/resource*/hist_bill + 治理字段）
│   └── load_pg.py                  #   JSONL → PG 幂等导入（-m cost.load_pg）
│
│  ── utils / tools ──
├── utils/                          # tokenizer（字符级分词）/ text_cleaner / logger
└── tools/                          # 评测 / 审核 / 运维（-m tools.X 运行）
    ├── retrieve_cli.py             #   混合检索 CLI（薄封装 HybridRetriever）
    ├── eval.py                     #   检索质量评测（按包含关系判命中；⚠️ T10 待换指标）
    ├── review_quality.py           #   质量审核（⚠️ 仍 v1，未适配 chunks.json，T10 待改）
    └── setup_server.sh / rename_raw_files.sh   #   运维脚本
```

> **运行模型**：ce-code 不安装为包（`packages=[]`），**从 ce-code 根运行**。构建 `python build.py …`；
> 阶段 0 解析与服务/工具用模块式 `python -m parser mineru …` / `python -m service.knowledge_api` /
> `python -m tools.eval …`。各层绝对 import（`from ir import Chunk` / `import splitter`），无 sys.path hack。
> ★=本轮实现、◌=占位（未实装抛 NotImplementedError）。

---

## 基础设施

知识层运行依赖以下已部署服务（**完整地址/版本约束/用途见 `DEV.md`**）：

| 服务 | 地址                         |
|---|----------------------------|
| Embedding（bge-large-zh-v1.5） | `http://172.19.3.136:8097` |
| VLM（Qwen2.5-VL-7B，PDF 图示理解） | `http://172.19.3.136:8098`    |
| 文本生成（Qwen3-8B，查询改写/LLM 校验） | `http://172.19.3.136:8099`    |
| 向量库（Milvus） | `http://172.19.3.136:19530`   |

---

## Pipeline脚本

所有脚本在 `ce-code/` 目录下用项目 venv 执行：`uv run python ...`。依赖管理：`uv add <package>`（装新依赖，写入 `pyproject.toml`）、`uv sync`（同步环境到 `pyproject.toml`/`uv.lock`）。

### Step 1 — 服务器一次性环境准备

```bash
bash tools/setup_server.sh
```

### Step 2 — 放 PDF

把规范 PDF 放到 `data/raw/`（如 `data/raw/GB_50016-2014(2018年版)_建筑设计防火规范.pdf`）。

### Step 3 — PDF 解析

`python -m parser mineru` **走远程 MinerU API**（`172.19.2.2:8000`，热服务 + `hybrid-auto-engine` 现成可用，无需本地 GPU/MinerU 环境）。环境差异详见 `DEV.md`「MinerU 解析」。

> backend 选择：定额/造价类含密集表格的文档用 `hybrid-auto-engine`（表格逐列对位，默认）；`--backend pipeline` 更快但密集表格会列错位。

整本一次解析（API 主机资源充足，无本地 OOM 问题）：

```bash
uv run python -m parser mineru --pdf data/raw/<文件名>.pdf
```

也可直接 curl（同步返回 JSON，`results.<文件名>.md_content` / `.content_list`；调试单页用 `start_page_id`/`end_page_id`）：

```bash
curl -s -X POST http://172.19.2.2:8000/file_parse -F "files=@data/raw/<文件名>.pdf;type=application/pdf" -F "backend=hybrid-auto-engine" -F "lang_list=ch" -F "table_enable=true" -F "return_md=true" -F "return_content_list=true"
```

输出落在 `data/parsed/<basename>/auto/`，含 `.md` 和 `_content_list.json`，直接传给 Step 4。

#### 解析产物说明（`.md` vs `_content_list.json` vs `images/`）

同一次解析有三份产物，用途不同：

| 产物 | 给谁 | 形态 |
|---|---|---|
| `<basename>.md` | **人**（对照原 PDF 做质量 review，见 Step 5 评估维度） | 全文按阅读顺序渲染成 markdown：标题 `#`、表格内联成表格文字、插图 `![](images/..)`、公式 `$..$` |
| `<basename>_content_list.json` | **程序**（build.py 切分层吃的是它） | 分块列表，每块带 `type`(text/title/table/image/equation)、`text_level`(标题层级)、`page_idx`、`bbox` 坐标 |
| `images/` | — | 从 PDF 切出的位图（插图、以及**被裁成图的表格**），上面两份只引用路径、不内嵌字节 |

> 切分层必须用 json 而非 md：建节点树要知道「几级标题 / 第几页 / 是表格还是正文」，这些 md 拿不到。

**图片/表格在 json 里怎么体现（MinerU v1，字段均在顶层；由 `parser/mineru.py` 的 `FormatAdapter` 处理）**：

- **插图**：`type=image`，顶层 `img_path` + `image_caption`。md 里对应 `![](images/..)`。
- **表格**：`type=table`，**三存**——表格裁切图 + 结构化 `<table>` HTML（带 colspan/rowspan）+ 表题。字段：顶层 `table_body`(HTML 串) / `img_path` / `table_caption`(list[str])。md 只把 HTML 渲染成表格文字内联、**不引用**裁切图，所以「md 里看不到表格图路径、表格变成了文字」是正常现象。

> ✅ **表体提取（已实现）**：`parser/mineru.py` 的 `FormatAdapter` 从 `table_body` 取出表格 HTML，经 `_HTMLTableParser` + `_expand_spans` 解析为**矩形**二维表（展开 colspan/rowspan 防串列），随块落入 `body`，建树时挂到所属节点的 `tables[]`。

### Step 4 — 知识库构建（一次跑完 / 分步跑同源）

`build.py` 是命令组：`all` 一条命令跑完 **解析 → 切分建树 → 选粒度 → 表征 → 建索引**；`parse/split/view/feature/index` 五条分步子命令逐步审核。两者共用同一批 `step_*` 步骤函数、落同一批中间产物，故**一次跑完与分步跑产出逐字一致**。`standard_id` 默认取输入 basename；可加 `--standard-id "GB 50016-2014(2018)"` 固定。需 Milvus + embedding 服务：

```bash
uv run python build.py all --input "data/parsed/<basename>/auto/<basename>_content_list.json" --profile-name default --index-granularity clause --embed-url http://localhost:8097 --embed-model-id /model
```

中间产物按 profile 隔离落 `data/structured/<standard>/<profile>/`：① 解析 `document.json`（格式归一后的纯版面块流）→ ② 切分 `chunks.json`（Chunk 树·单一真值）+ `catalog_blocks.json`（目录打标快照·调试）→ ③ 选粒度 `units.json`（检索单元，clause=已接地叶）→ ④ 表征 `features.json`（sidecar，dense 向量留索引期填）→ ⑤ 索引落 `data/vector_store/<standard>/<profile>/`（bm25/metadata/Milvus，collection 名由 profile 推断，与 service/eval 一致）。可选 `--parser-strategy`（缺省 `mineru`）、`--structure-strategy`（缺省 `toc`）、切分深度 `--toc-max-depth` / `--subsplit`。无 Milvus 时加 `--bm25-only`（只建 BM25 + metadata）。

**分步跑（逐步审核中间产物）**：每条子命令从盘上前一步产物起跑、只跑一步、再落盘——用 `--standard-id` + `--profile-name` 定位 `structured/<std>/<profile>/` 目录：

```bash
uv run python build.py parse --input "data/parsed/<basename>/auto/<basename>_content_list.json"   # ① → document.json
uv run python build.py split --standard-id "<std>" --subsplit number                              # ② → chunks.json
uv run python build.py view --standard-id "<std>"                                                 # ③ → units.json
uv run python build.py feature --standard-id "<std>"                                              # ④ → features.json
uv run python build.py index --standard-id "<std>" --bm25-only                                    # ⑤ → store（去掉 --bm25-only 建 Milvus 向量）
```

**只跑到前面某步**（不必动 build）：阶段 0 MinerU 解析见 Step 3 的 `python -m parser`；**只切分建树、看节点树**（本地无需 Milvus）用切分层入口或预览：

```bash
uv run python -m splitter toc --input "data/parsed/<basename>/auto/<basename>_content_list.json"   # 解析+切分落 catalog_blocks.json + chunks.json
uv run python build.py all --input "data/parsed/<basename>/auto/<basename>_content_list.json" --preview  # 只打印前 20 条节点，不落盘
```

### Step 5 — 质量审核

> ⚠️ `tools/review_quality.py` 仍是 v1 口径（读旧 `_clauses.json`、统计强条），**尚未适配 chunks.json**，待 T10 改造成节点树健康检查（孤儿节点 / 空内容 / 表格归属 / 悬空引用）。当前流程可跳过此步。

### Step 6 — 检索验证

```bash
uv run python -m tools.retrieve_cli --store-dir data/vector_store/<standard>/<profile> --query "24米高的住宅楼疏散楼梯最小净宽度" --skip-rerank
```

评测集批量评测：`uv run python -m tools.eval --store-dir data/vector_store/<standard>/<profile>`。

> ⚠️ `tools/retrieve_cli` / `tools/eval` 仍含 v1 强条召回率口径，待 T10 换 Recall@k / 引用召回 / MRR（按包含关系判命中）。评测口径见 `TODO.md`。

---

## HTTP 服务脚本

```bash
# 知识服务（检索原语 /search /expand /clause，:8100）—— 必须先起（从 ce-code 根，模块式）
cd /mnt/nvme/calvin/code/deer-flow/ce-code && uv run python -m service.knowledge_api
curl http://localhost:8100/health
```

端到端问答/合规检查由任务层提供，示例：

```bash
curl -s http://localhost:8101/qa -H 'Content-Type: application/json' -d '{"query":"24米高的住宅楼疏散楼梯最小净宽度是多少？","standard":"gb50016"}'
```

---

## 造价数据轨（Phase C · `cost/`）

结构化造价数据（清单/定额/价格/历史）走**关系库 PostgreSQL** 作单一事实源，与上面的规范类 RAG 流水线解耦。库在服务器：容器 `ce-postgres`（端口 `5433`，库 `ce_cost`，用户 `cost`）。建表/导入已落成仓库内可复现脚本，**幂等可重跑**，不再手敲 psql。

> 依赖 `psycopg`：服务器首次跑前 `uv add 'psycopg[binary]'`（写入 `pyproject.toml`，勿 `uv pip install`）。
> 连接串带密码经环境变量传入、不写进仓库：`CE_PG_DSN='postgresql://cost:<密码>@localhost:5433/ce_cost'`（缺省回退 `postgresql://cost@localhost:5433/ce_cost`，密码走 libpq 的 `PGPASSWORD`/`.pgpass`）。

### Step C1 — 从节点树抽清单项规范（`cost/bill_spec.py`）

读切分层产物 `chunks.json`，按「表头含『项目编码』」双出口分流：清单项规范表 → `bill_spec.jsonl`（每行一条清单项，feature_schema / work_content 按编号拆 list）；辅助/参数表（土石分类表、工作面宽度表…）→ `aux_tables.jsonl`（列头异构不归一，原样留矩形 body 供 calc_rule 查表）。非 9 位编码的行落 `bill_spec_anomalies.jsonl` 供人工抽查、不入库。`spec_version` 由 `normalize_spec` 归一到 canonical 规范号（如 `GB/T 50854-2024`）并带 `doc_id`（如 `GB-50854`）。

> `chunks.json` 按 `<规范>/<profile>/` 隔离，须用 `--input` 指到具体路径（默认值是旧扁平位置，已过时）：

```bash
uv run python -m cost.bill_spec --input "data/structured/GB_T50854_2024_房屋建筑与装饰工程工程量计算标准/default/chunks.json"
```

产物落 `data/structured/bill_spec.jsonl` + `aux_tables.jsonl`，终端打印质量 report（数量 / 编码唯一性 / 连续性 / 单位受控 / 特征空率 / 辅助表清单）。`--dry-run` 只看 report 不落盘。

### Step C2 — 建表 + 幂等导入 PG（`cost/schema.sql` + `cost/load_pg.py`）

`schema.sql` 一次建齐全部造价表（`bill_spec` / `aux_table` / `quota_item` / `quota_resource` / `resource` / `resource_price` / `hist_bill`），强制治理字段 `doc_id`/`spec_version`/`region`/`effective_priority`（价格 `resource_price` 带 `effective_period` 时效 + `btree_gist` EXCLUDE 防重叠）；`IF NOT EXISTS` 幂等。`load_pg.py` 读 jsonl，按主键 `ON CONFLICT DO UPDATE` upsert（`bill_spec` 按 `code`、`aux_table` 按 `doc_id+caption+chapter`），可重复执行不产生重复行。

```bash
CE_PG_DSN='postgresql://cost:<密码>@localhost:5433/ce_cost' uv run python -m cost.load_pg --init-schema --bill-spec data/structured/bill_spec.jsonl --aux data/structured/aux_tables.jsonl
```

> ⚠️ `--init-schema` 是 `CREATE TABLE IF NOT EXISTS`，**不会改已存在的表结构**。若库里有缺治理字段的旧 `bill_spec`（早期手敲建的），先删了再灌（导入幂等，删表无损）：`docker exec ce-postgres psql -U cost -d ce_cost -c "DROP TABLE IF EXISTS bill_spec"`。

### Step C3 — 验收

```bash
docker exec ce-postgres psql -U cost -d ce_cost -c "SELECT doc_id, spec_version, region, effective_priority, count(*) FROM bill_spec GROUP BY 1,2,3,4"
```

GB/T 50854 当前应为 472 条、全 `GB-50854 / GB/T 50854-2024 / 全国 / 1`；`aux_table` 5 张。

> 连 `ce-postgres` 须走 rootless docker（`docker context use rootless` 或设 `DOCKER_HOST`），**绝不用 `sudo docker`**（那打到共用 daemon）。部署细节见 `DEV.md §6`。
