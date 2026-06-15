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
│  ── core：统一 IR 契约（@dataclass + to_dict/from_dict）──
├── core/                           # 各阶段中间表示（IR），全层只认这里
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
> `python -m tools.eval …`。各层绝对 import（`from core import Chunk` / `import splitter`），无 sys.path hack。
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

换 backend / 指定 API 地址：

```bash
uv run python -m parser mineru --pdf data/raw/<文件名>.pdf --backend pipeline --server-url http://172.19.2.2:8000
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

### Step 4 — 知识库构建（三阶段单独执行）

`build.py` 不再一条命令串到底，改用 **`--stage` 选只跑哪一阶段**：`structure`（解析+切分）→ `reprs`（挂表征）→ `index`（建索引）。**阶段间靠 `chunks.json` 落盘解耦**——后一阶段读前一阶段产出的 `data/structured/<standard>/<profile>/chunks.json` 续跑，可分别重跑、互不重算。

三阶段都传**同一个 `--input`**（content_list 路径）与**同一个 `--profile-name`**，`standard_id` 默认取输入 basename（三阶段据此对齐到同一产物目录）；可加 `--standard-id "GB 50016-2014(2018)"` 固定。依赖顺序：structure → reprs → index（缺前一阶段产物会报错提示先跑）。

> 留一个 `--input` 的代价是 reprs/index 不需要 content_list，只为对齐 `standard_id` / 产物目录；不想传可改用 `--standard-id` 显式指定（二者取其一对齐到 `data/structured/<standard>/<profile>/`）。

**阶段 ① structure**（解析 → 切分建树 → 落 `chunks.json` + `catalog_blocks.json`，看节点树）：

```bash
uv run python build.py --input "data/parsed/<basename>/auto/<basename>_content_list.json" --profile-name default --stage structure --structure-strategy toc
```

按 `--structure-strategy`（缺省 `toc`，基于 PDF 原生目录的多层级切分）选 splitter，输出落在 `data/structured/<standard>/<profile>/chunks.json`（Chunk 树·单一真值）+ `catalog_blocks.json`（调试）。可选 `--parser-strategy`（缺省 `mineru`）、切分深度 `--toc-max-depth` / `--subsplit`。只想快速预览不落盘用 `--preview`（打印前 20 条节点）。

**阶段 ② reprs**（读 `chunks.json` → `feature.enrich` 挂免费 4 项 → 原地重写带表征的 `chunks.json`，无外部服务依赖）：

```bash
uv run python build.py --input "data/parsed/<basename>/auto/<basename>_content_list.json" --profile-name default --stage reprs
```

**阶段 ③ index**（读带表征 `chunks.json` → `index.view` 选粒度 emit → BM25 + 向量；需 Milvus + embedding 服务）：

```bash
uv run python build.py --input "data/parsed/<basename>/auto/<basename>_content_list.json" --profile-name default --stage index --index-granularity clause --embed-url http://localhost:8097 --embed-model-id /model
```

`index.view` 选粒度（当前仅 `clause`）→ emit；输出按 profile 隔离落 `data/vector_store/<standard>/<profile>/`，Milvus collection 名由 profile 推断（与 service/eval 一致）。无 Milvus 时加 `--bm25-only`（只建 BM25 + metadata）。index 阶段消费 reprs 挂的 `sparse`/`dense` 表征，未先跑 reprs 会报错提示。

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
