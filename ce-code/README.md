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
│   ├── parsed/                     #   MinerU 解析输出
│   ├── structured/                 #   条款树 JSON（02 脚本输出）
│   ├── vector_store/               #   BM25 + Milvus 索引（04 脚本输出）
│   ├── eval_set/                   #   评测集（入 git）
│   │   └── gb50016_eval.json       #     GB 50016 的 45 条评测用例
│   └── quality_reports/            #   质量审核报告（03 脚本输出）
├── pipeline/                       # 数据流水线（解析 → 条款树 → 建索引）
│   ├── setup_server.sh             #   服务器一次性环境准备
│   ├── rename_raw_files.sh         #   原始 PDF 重命名工具
│   ├── mineru_api.py               #   远程 MinerU API 客户端（默认解析方式）
│   ├── 01_parse_pdf.py             #   PDF 解析入口（默认走 API，--local 用本地 CLI）
│   ├── split_and_parse.py          #   大 PDF 分块解析（仅 --local 路径，规避本地 OOM）
│   ├── 02_extract_clauses.py       #   构建条款树（章/节/条层级）
│   ├── 03_review_quality.py        #   条款树质量审核与报告
│   └── 04_build_index.py           #   建 BM25 + Milvus 向量双索引
├── scripts/                        # 检索层薄 CLI（只依赖 retrieval）
│   ├── 05_retrieve.py              #   混合检索 + 引用扩展 + Rerank
│   └── 07_eval.py                  #   检索质量评测（强条召回率）
├── retrieval/                      # 检索引擎库（纯检索，被服务/脚本共用）
│   ├── config.py                  #   默认配置、规范别名、store/collection 解析
│   └── engine.py                  #   混合检索 search() + 引用扩展 + rerank + get_clause
└── service/
    └── server.py                  #   知识服务 :8100 —— 仅原语 /search /expand /clause /health
```

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
bash pipeline/setup_server.sh
```

### Step 2 — 放 PDF

把规范 PDF 放到 `data/raw/`（如 `data/raw/GB_50016-2014(2018年版)_建筑设计防火规范.pdf`）。

### Step 3 — PDF 解析

`01_parse_pdf.py` **默认走远程 MinerU API**（`172.19.2.2:8000`，热服务 + `hybrid-auto-engine` 现成可用），加 `--local` 才用本地 CLI。两条路径是同一套 MinerU，同 backend 输出逐字一致；选型与环境差异详见 `DEV.md`「MinerU 两种解析方式」。

> backend 选择：定额/造价类含密集表格的文档用 `hybrid-auto-engine`（表格逐列对位，默认）；`--backend pipeline` 更快但密集表格会列错位。本地 CLI（`--local`）跑 hybrid 需先修好 venv 的 vllm（见 `DEV.md`），否则只能用 pipeline。

#### 默认 — 远程 API（推荐，无需本地 GPU/MinerU 环境）

整本一次解析（API 主机资源充足，无本地 OOM 问题，无需分块）：

```bash
uv run python pipeline/01_parse_pdf.py --pdf data/raw/<文件名>.pdf
```

换 backend / 指定 API 地址：

```bash
uv run python pipeline/01_parse_pdf.py --pdf data/raw/<文件名>.pdf --backend pipeline --server-url http://172.19.2.2:8000
```

也可直接 curl（同步返回 JSON，`results.<文件名>.md_content` / `.content_list`；调试单页用 `start_page_id`/`end_page_id`）：

```bash
curl -s -X POST http://172.19.2.2:8000/file_parse -F "files=@data/raw/<文件名>.pdf;type=application/pdf" -F "backend=hybrid-auto-engine" -F "lang_list=ch" -F "table_enable=true" -F "return_md=true" -F "return_content_list=true"
```

> API 每次调用都重传整个 PDF，分段解析会重复上传；省带宽走 `--local`。

#### `--local` — 本地 CLI（离线 / 省带宽大批量）

小 PDF（≤100 页）整本一次解析：

```bash
CUDA_VISIBLE_DEVICES=2 HF_ENDPOINT=https://hf-mirror.com uv run python pipeline/01_parse_pdf.py --pdf data/raw/<文件名>.pdf --local --backend pipeline
```

大 PDF（>100 页）分块解析，规避本地显存 OOM（GB 50016 共 464 页，用 80 页/块）：

```bash
CUDA_VISIBLE_DEVICES=2 HF_ENDPOINT=https://hf-mirror.com uv run python pipeline/split_and_parse.py --pdf data/raw/<文件名>.pdf --chunk-size 80
```

无论哪种方式，输出都落在 `data/parsed/<basename>/auto/`，含 `.md` 和 `_content_list.json`，直接传给 Step 4。

#### 解析产物说明（`.md` vs `_content_list.json` vs `images/`）

同一次解析有三份产物，用途不同：

| 产物 | 给谁 | 形态 |
|---|---|---|
| `<basename>.md` | **人**（对照原 PDF 做质量 review，见 Step 5 评估维度） | 全文按阅读顺序渲染成 markdown：标题 `#`、表格内联成表格文字、插图 `![](images/..)`、公式 `$..$` |
| `<basename>_content_list.json` | **程序**（02 提取条款树吃的是它） | 分块列表，每块带 `type`(text/title/table/image/equation)、`text_level`(标题层级)、`page_idx`、`bbox` 坐标 |
| `images/` | — | 从 PDF 切出的位图（插图、以及**被裁成图的表格**），上面两份只引用路径、不内嵌字节 |

> 02 必须用 json 而非 md：建条款树要知道「几级标题 / 第几页 / 是表格还是正文」，这些 md 拿不到。

**图片/表格在 json 里怎么体现（只认 MinerU v1，字段均在顶层；02 由 `read_v1` 处理）**：

- **插图**：`type=image`，顶层 `img_path` + `image_caption`。md 里对应 `![](images/..)`。
- **表格**：`type=table`，**三存**——表格裁切图 + 结构化 `<table>` HTML（带 colspan/rowspan）+ 表题。字段：顶层 `table_body`(HTML 串) / `img_path` / `table_caption`(list[str])。md 只把 HTML 渲染成表格文字内联、**不引用**裁切图，所以「md 里看不到表格图路径、表格变成了文字」是正常现象。

> ℹ️ MinerU 另有 v2（按页嵌套、字段包在 `content` 下）格式，但本项目管线（mineru_api.py）只产出 v1，且 v1 顺序更可靠、取值更直接，故 02 已移除 v2 读取分支，只保留 `read_v1`。

> ✅ **表体提取（已实现）**：`read_v1` 从 `table_body` 取出表格 HTML，经 `_HTMLTableParser` + `_expand_spans` 解析为**矩形**二维表（展开 colspan/rowspan 防串列），随条款落入 `tables[].body`；表格裁切图路径落 `images[]`/`tables` 关联。
>
> 解析层还处理了几个真实坑（GB/T 50500-2024 实测）：v1 `list` 多条款拆分（如 1.0.1~1.0.7 各自成条款）、目录(TOC)整列/短行剔除（含中英文目录，避免与正文条款重复）、交叉引用片段（"8.3节、…"）不误建条款、**附录字母条号识别**（`E.1`/`E.2.2` 各自成条款，表格精确归位到子条款而非堆在附录根）。
>
> 仍属 Phase B 波2 的是**构建层**封装：把 `tables[].body` 升级为可「给定行列取值」的 `schema.TableRepr`（分表头、继承所属条款强制性），见 `extract/`。

### Step 4 — 提取条款树

```bash
uv run python pipeline/02_extract_clauses.py --input "data/parsed/<basename>/auto/<basename>_content_list.json" --standard-id "GB 50016-2014(2018)" --output-dir data/structured/
```

输出落在 `data/structured/<standard>_clauses.json`。

### Step 5 — 质量审核

```bash
uv run python pipeline/03_review_quality.py --input data/structured/<standard>_clauses.json --standard-id "GB 50016-2014(2018)" --check-issues --export-report
```

报告输出至 `data/quality_reports/`。查看单条款：

```bash
uv run python pipeline/03_review_quality.py --input data/structured/<standard>_clauses.json --show-clause 5.3.1
```

### Step 6 — 建双索引（BM25 + 向量）

```bash
uv run python pipeline/04_build_index.py --input data/structured/GB_50016-20142018_clauses.json --embed-url http://localhost:8097 --embed-model-id /model
```

输出落在 `data/vector_store/GB_50016_20142018/`，Milvus collection 名为 `building_code_gb_50016_20142018`。

### Step 7 — 检索验证

```bash
uv run python scripts/05_retrieve.py --store-dir data/vector_store/GB_50016_20142018 --query "24米高的住宅楼疏散楼梯最小净宽度" --skip-rerank
```

核心指标（强条召回率等评测口径）见 `TODO.md`。

---

## HTTP 服务脚本

```bash
# 知识服务（检索原语 /search /expand /clause，:8100）—— 必须先起
cd /mnt/nvme/calvin/code/deer-flow/ce-code && uv run python service/server.py
curl http://localhost:8100/health
```

端到端问答/合规检查由任务层提供，示例：

```bash
curl -s http://localhost:8101/qa -H 'Content-Type: application/json' -d '{"query":"24米高的住宅楼疏散楼梯最小净宽度是多少？","standard":"gb50016"}'
```
