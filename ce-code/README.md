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
├── .gitignore                      # 仅忽略 raw/ (PDF) + vector_store/ (大体积索引)
├── data/                           # 数据资产（parsed/structured/eval_set 入 git；raw/vector_store 不入）
│   ├── raw/                        #   原始 PDF（手动放入；⛔不入 git，版权敏感）
│   ├── parsed/                     #   MinerU 解析输出（python -m parser 产物，阶段 0 缓存；✅入 git）
│   ├── structured/                 #   <规范名>/default/chunks.json（build）+ <doc_id>/<表>.jsonl（cost 按 doc_id 分目录）+ 扁平 bill_quota_map.jsonl；✅入 git
│   ├── vector_store/               #   BM25 + Milvus 索引（build 索引层产物；⛔不入 git，大体积可重生）
│   ├── eval_set/                   #   评测集（✅入 git）
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
│   ├── price_composition.py        #   50500 chunks.json → price_composition.jsonl（费用构成规则，正则锚定原文）
│   ├── quota.py                    #   SJG chunks.json → quota_item/resource/quota_resource.jsonl（定额转置矩阵·单位格锚定）
│   ├── bill_quota.py               #   bill_spec + quota_item → bill_quota_map.jsonl（清单→定额 APPLIES 名称匹配·KG P0）
│   ├── price.py                    #   信息价 chunks.json → resource_price.jsonl（物料月度价 + 时效，动态独立管道）
│   ├── fee_rate.py                 #   费率标准 chunks.json → fee_rate.jsonl（费率参考范围 + 推荐值，声明式规则）
│   ├── schema.sql                  #   全表 DDL（bill_spec/aux_table/price_composition/quota_*/resource*/resource_price/fee_rate/bill_quota_map/hist_bill + 治理字段）
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

产物按 doc_id 分目录落 `data/structured/<doc_id>/bill_spec.jsonl` + `aux_tables.jsonl`（doc_id 从记录推断，多规范不互相覆盖），终端打印质量 report（数量 / 编码唯一性 / 连续性 / 单位受控 / 特征空率 / 辅助表清单）。`--dry-run` 只看 report 不落盘。

> **同码多行收口（`resolve_dups`）**：清单 PG 主键是 `code`，同码多行须收口——① **同名多单位**（规范一码配多个可选计量单位，如金属结构刷油 kg/m²）→ 合并成一行，`unit_options`(JSONB) 收全部单位、`unit` 取首个；② **异名撞码**（不同清单项撞同一 code，多为源 PDF/MinerU 编码读错）→ 该 code 全部行路由到 `bill_spec_conflicts.jsonl`、**不进主表**（宁缺毋造，不猜正确编码），报告供人工核对。GB 50856 通用安装已据此入库：1189 行 → 合并 4 多单位 + 出 2 冲突行（`031003010` 倒流防止器/淋浴器）→ **主表 1183（PK 零重复）** + aux 15 + anomalies 3 + conflicts 2。GB 50854 无重复，收口为 no-op。

### Step C1b — 从 50500 抽费用构成规则（`cost/price_composition.py`）

2024 版 GB 50500 已无清单项目录（搬到 50854），只剩计价规则正文。本步抽组价要程序化读的**费用构成**：声明式规则集 `RULES`（`node_path` + 正则）锚定 50500 原文单句，正则抽出构成项列表，**不中即报错（宁缺毋造）**。产 `price_composition.jsonl`（每行一个构成项，带 provenance + `doc_id`/`spec_version`）：综合单价（2.0.9）= 人工费/材料费/施工机具使用费/管理费/利润/风险（不含增值税）；工程造价（3.1.2）= 分部分项/措施项目/其他项目/增值税（2024 版四部分）。加新构成只需在 `RULES` 追加一条。

```bash
uv run python -m cost.price_composition --input "data/structured/GB_T50500_2024_建设工程工程量清单计价标准/default/chunks.json"
```

### Step C1c — 从 SJG 消耗量标准抽定额三表（`cost/quota.py`）

SJG 171/170 的定额子目表是**转置矩阵**（列=定额子目，行=属性+工料机），MinerU 矩形化后值的列位仍随标签层级深浅错位。`quota.py` 用**单位格锚定**（每行第一个数值/破折号前一格是单位，其后 N 格=N 个子目值）解析，双出口三表：`quota_item.jsonl`（子目编号/名称含变体/单位/工作内容 + 人材机费 + 综合单价 base_price）、`resource.jsonl`（人材机去重）、`quota_resource.jsonl`（子目×资源含量，natural key 链接，`load_pg` 解析成 FK）。`—`（不适用）跳过；价格列（2023-08 参考价）按决策不取（价格主源走信息价月刊独立管道）。非定额表（系数/厚度表）暂归 aux 口径。

> 须先 `build split` 出 SJG 的 chunks.json（SJG 无规整目录，`chapter`/`ancestor_titles` 可能偏弱，入库后抽查）：

```bash
uv run python -m cost.quota --input "data/structured/SJG_建筑工程消耗量标准/default/chunks.json"
uv run python -m cost.quota --input "data/structured/SJG_土石方与地基基础工程消耗量标准/default/chunks.json"
```

产物按 doc_id 分目录落 `data/structured/<doc_id>/{quota_item,resource,quota_resource}.jsonl`（SJG171→`SZ-SJG171/`、SJG170→`SZ-SJG170/`，互不覆盖），终端打印总览（子目/资源/含量数、跨页续表合并数、无费用子目、资源类别分布）。`--dry-run` 只看 report。同一定额子目跨页（续表/续前）会被 MinerU 拆成两行，`extract` 按 `(doc_id, quota_code)` 合并（优先非空费用），避免续前页 null 价覆盖首页真值。当前：SJG171 = 640 子目 / 407 资源 / 4173 含量；SJG170 = 617 子目（合并 1 续前）/ 584 资源 / 4105 含量。

### Step C1d — 清单→定额映射（KG P0，`cost/bill_quota.py`）

清单（GB 50854，9 位码）与定额（SJG，6 位+变体）编码不可互推，映射是组价关键一跳。P0 用**名称匹配**自动种子：清单名==定额名首段 → conf 0.9，清单名⊂首段 → conf 0.6（1 清单 : N 定额）。**扫 `data/structured/<doc_id>/` 下全部 bill_spec + quota_item 跨规范汇总匹配**（SJG171 建筑 + SJG170 土方一起），产扁平 `data/structured/bill_quota_map.jsonl`（跨规范关系产物，带 `relation=APPLIES`/`confidence`/`source`）。这是**起步映射**（含 SJG171+170 后覆盖 53/472、313 边），未覆盖/低置信项待富化（语义召回/专家标注），按红线「只建议不定稿」交任务层 HITL。

```bash
uv run python -m cost.bill_quota   # 默认扫 data/structured/<doc_id>/，--struct-dir 可改根
```

### Step C1e — 信息价 → 价格库（动态独立管道，`cost/price.py`）

深圳信息价（月刊）是**动态数据**，与定额/规范的静态口径解耦：`resource_price` 带 `effective_period` 时效，按 region + 期取价，不参与 `effective_priority` 排序。`price.py` 从信息价 `chunks.json` 抽价目表（`序号|材料名称|型号、规格|单位|价格(元)` 等变体），**列名子串定位**（名称列：设备名称→机械 / 项目名称→人工 / 否则材料；含「价格」+「元」且非「公式」→price），分类行（「一、黑色及有色金属」）记为 `sub_category`；天然排除价格指数（月份列）/ 造价对比 / 系数表 / 混凝土公式价（`价格计算公式(元)`）。时效从 standard_id「2026-5」推 `[2026-05-01, 2026-06-01)`（`--period YYYY-MM` 可覆盖）。产 per-doc `data/structured/SZ-JGXX-PRICE/resource_price.jsonl`（含物料自然键供 `load_pg` upsert 进 `resource` 取 id + price + 时效 + 溯源）。

```bash
uv run python -m cost.price --input "data/structured/2026_5深圳信息价_www_zgjct_com下载/default/chunks.json"
```

当前 2026-05：56 价目表 / 1138 价目行（材料 1024 / 机械 96 / 人工 35，同期多价去重 17），17 个分类。⚠️ 信息价物料名（如「建筑废渣混凝土实心砖 240x115x53」）与定额 resource 名（「普通混凝土实心砖 240×115×53」）格式有差，精确命中有限 → 大多作为**信息价自有资源**入 `resource`（doc_id=SZ-JGXX-PRICE），与定额 resource 的对接（语义匹配）待富化，按红线「只建议不定稿」。

### Step C1f — 计价费率标准 → 费率库（`cost/fee_rate.py`）

费率是「综合单价之上算工程造价」的乘数（安全文明施工/夜间施工/赶工/总承包服务费/增值税/附加税费/工程保险费）。费率标准 7 张费率表表头**各不相同**（专业工程/工程类别/费用名称\\系数/项目名称，单位 %/‰/系数混用），表少而杂 → 用**声明式规则 `RULES`**（按 caption 锚定每表的列布局 + 费用元数据），caption 不中任何规则即跳过并计数（**宁缺毋造**，不猜列）。产 per-doc `data/structured/SZ-FLBZ-2023/fee_rate.jsonl`（fee_category / fee_name / applicable / ref_low / ref_high / recommended / unit + 治理字段 + provenance）。

```bash
uv run python -m cost.fee_rate --input "data/structured/深圳市建设工程计价费率标准2023/default/chunks.json"
```

当前：7 费率表 / 2 跳过（安文费清单列项表 + 附录 B 包含内容，非费率）/ **24 费率行**（安文费 11 / 总承包服务费 3 / 附加税费 3 / 工程保险费 3 / 赶工 2 / 夜间施工 1 / 增值税 1），0 异常。

### Step C2 — 建表 + 幂等导入 PG（`cost/schema.sql` + `cost/load_pg.py`）

`schema.sql` 一次建齐全部造价表（`bill_spec` / `aux_table` / `price_composition` / `quota_item` / `quota_resource` / `resource` / `resource_price` / `fee_rate` / `bill_quota_map` / `hist_bill`），强制治理字段 `doc_id`/`spec_version`/`region`/`effective_priority`（价格 `resource_price` 带 `effective_period` 时效 + `btree_gist` EXCLUDE 防重叠；`resource` 唯一键 `NULLS NOT DISTINCT` 让 `spec=NULL` 也算同行）；`IF NOT EXISTS` 幂等。`load_pg.py` 读 jsonl，按主键 `ON CONFLICT DO UPDATE` upsert（`bill_spec` 按 `code`、`aux_table` 按 `doc_id+caption+chapter`、`price_composition` 按 `doc_id+composite+seq`、`quota_item` 按 `region+quota_code+spec_version`、`resource` 按 `category+name+spec+unit`），可重复执行不产生重复行。

```bash
# --scan-dir 自动扫各 <doc_id>/ 子目录全表 + 扁平 bill_quota_map，按依赖序一把灌
# （resource/quota_item 先于 quota_resource，bill_spec 先于 bill_quota_map）
CE_PG_DSN='postgresql://cost:<密码>@localhost:5433/ce_cost' uv run python -m cost.load_pg --init-schema --scan-dir data/structured
```

> 单文件 `--bill-spec` / `--quota-item` / `--resource-price` 等选项仍在（targeted 灌某表/某规范），可与 `--scan-dir` 叠加。预期计数：bill_spec 1655（50854 472 + 50856 1183）/ aux 20（5+15）/ price_composition 10 / resource 991+（SJG171 407 + SJG170 584，再并入信息价新物料）/ quota_item 1257（640+617）/ quota_resource 8278（4173+4105，跳过 0）/ resource_price 1138（信息价 2026-05；EXCLUDE 约束按 doc_id+期先删后插、同月重跑幂等）/ fee_rate 24 / bill_quota_map 313。

> ⚠️ `--init-schema` 是 `CREATE TABLE IF NOT EXISTS`，**不会改已存在的表结构**。若库里有缺治理字段的旧 `bill_spec`（早期手敲建的），先删了再灌（导入幂等，删表无损）：`docker exec ce-postgres psql -U cost -d ce_cost -c "DROP TABLE IF EXISTS bill_spec"`。

### Step C3 — 验收

```bash
docker exec ce-postgres psql -U cost -d ce_cost -c "SELECT doc_id, spec_version, region, effective_priority, count(*) FROM bill_spec GROUP BY 1,2,3,4"
docker exec ce-postgres psql -U cost -d ce_cost -c "SELECT composite, kind, string_agg(component, ' / ' ORDER BY seq) FROM price_composition GROUP BY 1,2"
docker exec ce-postgres psql -U cost -d ce_cost -c "SELECT (SELECT count(*) FROM quota_item) AS 子目, (SELECT count(*) FROM resource) AS 资源, (SELECT count(*) FROM quota_resource) AS 含量, (SELECT count(*) FROM bill_quota_map) AS 映射边"
```

**组价取数路径 demo**（清单 → 定额 → 工料机含量，验证底座可用）：

```bash
docker exec ce-postgres psql -U cost -d ce_cost -c "SELECT b.code 清单, q.quota_code 定额, q.name 定额名, r.category 类, r.name 工料机, qr.consumption 含量, r.unit FROM bill_spec b JOIN bill_quota_map m ON m.bill_code=b.code JOIN quota_item q ON q.quota_code=m.quota_code AND q.doc_id=m.quota_doc_id JOIN quota_resource qr ON qr.quota_id=q.id JOIN resource r ON r.id=qr.resource_id WHERE b.code='010401002' ORDER BY q.quota_code, r.category LIMIT 20"
```

GB/T 50854 当前应为 472 条、全 `GB-50854 / GB/T 50854-2024 / 全国 / 1`；`aux_table` 5 张；`price_composition` 2 个构成（综合单价 6 项 / 工程造价 4 部分，共 10 行）；SJG 171 建筑工程本地解析为 640 子目 / 407 资源 / 4173 含量、`bill_quota_map` 约 212 边覆盖 42 清单（服务器实跑以 chunks.json 为准）。取数 demo 应返回「清单 010401002 实心砖墙 → 6 定额子目 → 各自工料机含量」。

> 连 `ce-postgres` 须走 rootless docker（`docker context use rootless` 或设 `DOCKER_HOST`），**绝不用 `sudo docker`**（那打到共用 daemon）。部署细节见 `DEV.md §6`。
