# building-code-rag-poc

建筑规范 RAG 项目的技术 POC，验证 PDF 解析流水线可行性。

> 上下文与设计原则见仓库根目录 `CLAUDE.md`。本目录是 POC 阶段的独立沙箱，**不污染 deer-flow 既有结构**；跑通后再决定是否包装为 `skills/building-code-rag/`。

---

## 目录结构

```
building-code-rag-poc/
├── README.md
├── pyproject.toml              # uv 管理依赖（沿用 deer-flow 风格）
├── .gitignore                  # 忽略 data/ 下的 PDF 与解析产物
├── data/
│   ├── raw/                    # 放原始 PDF（手动放入，不入 git）
│   ├── parsed/                 # MinerU 解析输出（不入 git）
│   ├── structured/             # 条款树 JSON（02 脚本输出，不入 git）
│   └── quality_reports/        # 质量审核报告（03 脚本输出，不入 git）
├── scripts/
│   ├── setup_server.sh         # 服务器一次性环境准备
│   ├── 01_parse_pdf.py         # 阶段 0 第一步：MinerU 解析
│   ├── 02_extract_clauses.py   # 阶段 0 第二步：构建条款树
│   ├── 03_review_quality.py    # 阶段 0 第三步：质量审核
│   ├── 04_build_index.py       # 阶段 1 第一步：建 BM25 + 向量双索引
│   └── 05_retrieve.py          # 阶段 1 第二步：混合检索 + 引用扩展 + Rerank
└── notebooks/                  # 调试与 review 用的 ipynb
```

---

## 阶段 0：PDF 解析 POC（当前阶段）

**目标**：拿 GB 50016 的 1-2 章过完整解析流水线，人工 review 抽取质量，**目标 >95% 准确率**。不达标不进入后续阶段。

### 评估维度（必看）

| 维度 | 怎么算合格 |
|---|---|
| 条款边界 | "3.4.1"、"3.4.2" 被正确切分为独立条目，层级关系正确 |
| 表格 | 表格被结构化（行列关系保留），不是扁平化文本 |
| 公式 | 公式抽出为 LaTeX 或可识别格式，不是乱码 |
| 引用 | "应符合 X.X.X 的规定" 这类引用能被识别 |
| 强制性标识 | "必须 / 严禁 / 不应 / 不得" 被标注，"宜 / 可" 区分 |
| 图示 | 图示被切出（哪怕只是位置框），可在后续步骤中由 VLM 描述 |

---

## 使用流程

### 1. 服务器一次性环境准备
```bash
bash scripts/setup_server.sh
```

### 2. 放 PDF
把规范 PDF 放到 `data/raw/`（如 `data/raw/GB50016-2014-2018.pdf`）

### 3. 跑解析
```bash
HF_ENDPOINT=https://hf-mirror.com uv run python scripts/01_parse_pdf.py \
  --pdf data/raw/<文件名>.pdf
```

输出落在 `data/parsed/<pdf-basename>/auto/`，含 `.md` 和 `_content_list.json`。

### 4. 提取条款树
```bash
uv run python scripts/02_extract_clauses.py \
  --input data/parsed/<pdf-basename>/auto/<pdf-basename>_content_list.json \
  --standard-id "GB 50016-2014(2018)"
```

输出落在 `data/structured/<standard>_clauses.json`。

### 5. 质量审核
```bash
uv run python scripts/03_review_quality.py \
  --input data/structured/<standard>_clauses.json \
  --standard-id "GB 50016-2014(2018)" \
  --check-issues \
  --export-report
```

报告输出至 `data/quality_reports/<standard>_report.md`。

查看单条款：
```bash
uv run python scripts/03_review_quality.py \
  --input data/structured/<standard>_clauses.json \
  --show-clause 5.3.1
```

### 6. 人工 review
对照原 PDF 抽查关键条款，按"评估维度"评分，确认达标后进入阶段 1。

---

## 阶段 1：MVP 条文检索

### 7. 安装检索依赖
```bash
uv pip install -e ".[retrieval]"
```

### 8. Milvus 已在服务器部署（端口 19530），无需额外启动。

### 9. 建立双索引（BM25 + 向量）
```bash
HF_ENDPOINT=https://hf-mirror.com uv run python scripts/04_build_index.py \
  --input data/structured/GB_50016-20142018_clauses.json \
  --standard-id "GB 50016-2014(2018)" \
  --milvus-host localhost \
  --milvus-port 19530
```

输出落在 `data/vector_store/GB_50016-20142018/`，Milvus collection 名为 `building_code_gb_50016-20142018`。

只建 BM25（跳过向量索引）：
```bash
uv run python scripts/04_build_index.py \
  --input data/structured/GB_50016-20142018_clauses.json \
  --bm25-only
```

### 10. 检索验证
```bash
# 单条查询
uv run python scripts/05_retrieve.py \
  --store-dir data/vector_store/GB_50016-20142018 \
  --query "24米住宅疏散楼梯净宽"

# 跳过 Rerank（调试，速度更快）
uv run python scripts/05_retrieve.py \
  --store-dir data/vector_store/GB_50016-20142018 \
  --query "防火分区最大建筑面积" \
  --skip-rerank

# 批量评测（需先核实 eval_set）
uv run python scripts/05_retrieve.py \
  --store-dir data/vector_store/GB_50016-20142018 \
  --eval-set data/eval_set/gb50016_eval.json
```

核心指标：**强条召回率**（`avg_mandatory_recall`）——宁可多召回，不能漏强条。
