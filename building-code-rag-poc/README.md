# building-code-rag-poc

建筑规范 RAG 项目的技术 POC，验证 PDF 解析 → 条款提取 → 混合检索 → 结构化生成的完整流水线。

> 上下文与设计原则见仓库根目录 `CLAUDE.md`。本目录是 POC 阶段的独立沙箱，**不污染 deer-flow 既有结构**；跑通后再决定是否包装为 `skills/building-code-rag/`。

---

## 目录结构

```
building-code-rag-poc/
├── README.md
├── pyproject.toml                  # uv 管理依赖
├── .gitignore                      # 忽略 data/ 下的大文件与解析产物
├── data/
│   ├── raw/                        # 原始 PDF（手动放入，不入 git）
│   ├── parsed/                     # MinerU 解析输出（不入 git）
│   ├── structured/                 # 条款树 JSON（02 脚本输出，不入 git）
│   ├── vector_store/               # BM25 + Milvus 索引（04 脚本输出，不入 git）
│   ├── eval_set/                   # 评测集（入 git）
│   │   └── gb50016_eval.json       # GB 50016 的 45 条评测用例
│   └── quality_reports/            # 质量审核报告（03 脚本输出，不入 git）
└── scripts/
    ├── setup_server.sh             # 服务器一次性环境准备
    ├── rename_raw_files.sh         # 原始 PDF 重命名工具
    ├── split_and_parse.py          # 大 PDF 分块 MinerU 解析（推荐入口）
    ├── 01_parse_pdf.py             # 单文件 MinerU 解析（小 PDF 用）
    ├── 02_extract_clauses.py       # 构建条款树（章/节/条层级）
    ├── 03_review_quality.py        # 条款树质量审核与报告
    ├── 04_build_index.py           # 建 BM25 + Milvus 向量双索引
    ├── 05_retrieve.py              # 混合检索 + 引用扩展 + Rerank
    └── 06_generate.py              # 结构化生成（Qwen3-8B，强制引用条文号）
```

---

## 服务依赖（服务器已部署）

| 服务 | 地址 | 用途 |
|---|---|---|
| vLLM BGE-large | `http://localhost:8097` | 嵌入，model_id=`/model`，dim=1024 |
| vLLM Qwen3-8B | `http://localhost:8099` | 文本生成，model_id=`qwen3-8b` |
| vLLM Qwen2.5-VL-7B | `http://localhost:8098` | 图示理解，model_id=`/model` |
| Milvus | `http://localhost:19530` | 向量存储 |

---

## 使用流程

所有脚本在 `building-code-rag-poc/` 目录下执行，使用项目 venv：`.venv/bin/python`。

### Step 1 — 服务器一次性环境准备

```bash
bash scripts/setup_server.sh
```

### Step 2 — 放 PDF

把规范 PDF 放到 `data/raw/`（如 `data/raw/GB_50016-2014(2018年版)_建筑设计防火规范.pdf`）

### Step 3 — PDF 解析

**大 PDF（>100 页）推荐分块解析**（GB 50016 为 464 页，用 80 页/块）：

```bash
CUDA_VISIBLE_DEVICES=2 HF_ENDPOINT=https://hf-mirror.com \
  .venv/bin/python scripts/split_and_parse.py \
  --pdf data/raw/<文件名>.pdf --chunk-size 80
```

输出落在 `data/parsed/<pdf-basename>/auto/`，含 `.md` 和 `_content_list.json`。

### Step 4 — 提取条款树

```bash
.venv/bin/python scripts/02_extract_clauses.py \
  --input "data/parsed/<basename>/auto/<basename>_content_list.json" \
  --standard-id "GB 50016-2014(2018)" \
  --output-dir data/structured/
```

输出落在 `data/structured/<standard>_clauses.json`。

### Step 5 — 质量审核

```bash
.venv/bin/python scripts/03_review_quality.py \
  --input data/structured/<standard>_clauses.json \
  --standard-id "GB 50016-2014(2018)" \
  --check-issues \
  --export-report
```

报告输出至 `data/quality_reports/`。查看单条款：

```bash
.venv/bin/python scripts/03_review_quality.py \
  --input data/structured/<standard>_clauses.json \
  --show-clause 5.3.1
```

### Step 6 — 建双索引（BM25 + 向量）

```bash
.venv/bin/python scripts/04_build_index.py \
  --input data/structured/GB_50016-20142018_clauses.json \
  --embed-url http://localhost:8097 --embed-model-id /model
```

输出落在 `data/vector_store/GB_50016_20142018/`，Milvus collection 名为 `building_code_gb_50016_20142018`。

### Step 7 — 检索验证

```bash
.venv/bin/python scripts/05_retrieve.py \
  --store-dir data/vector_store/GB_50016_20142018 \
  --query "24米高的住宅楼疏散楼梯最小净宽度" \
  --skip-rerank
```

核心指标：**强条召回率**——宁可多召回，不能漏强条。

### Step 8 — 端到端生成

```bash
.venv/bin/python scripts/06_generate.py \
  --store-dir data/vector_store/GB_50016_20142018 \
  --query "24米高的住宅楼疏散楼梯最小净宽度是多少？"
```

输出结构化 JSON，含 `applicable_clauses`（带条文号）、`uncertain_aspects`、`out_of_scope_warnings`。

---

## 已验证规范

| 规范 | 条款数 | 强条数 | 索引状态 |
|---|---|---|---|
| GB 50378-2006（绿色建筑） | — | — | ✓ POC 验证用 |
| GB 50016-2014(2018)（建筑防火） | 911 | 592 | ✓ 已建索引 |

---

## 评估维度

| 维度 | 合格标准 |
|---|---|
| 条款边界 | "3.4.1"、"3.4.2" 正确切分，层级关系正确 |
| 强条识别 | 必须/严禁/不应/不得 → `is_mandatory=True`，宜/可 → False |
| 交叉引用 | "应符合 X.X.X 的规定" 被识别，`references_to` 字段有值 |
| 强条召回率 | Recall@20 on mandatory clauses > 目标阈值 |
| 引用扩展 | 命中条款的 `references_to` 被自动拉取 |
