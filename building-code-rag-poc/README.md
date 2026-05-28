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
    ├── 06_generate.py              # 结构化生成（Qwen3-8B，强制引用条文号）
    ├── 07_eval.py                  # 检索质量评测（强条召回率）
    ├── 08_extract_params.py        # 【阶段2】从自由文本提取结构化建筑参数
    ├── 09_gen_queries.py           # 【阶段2】按合规维度生成检索查询矩阵
    └── 10_compliance_check.py      # 【阶段2】端到端合规检查编排
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

## Skills（deer-flow 集成）

POC 脚本跑通后，检索与合规判定功能被封装为 deer-flow skills，可供任意 agent 通过 bash 工具调用。

所有 skill 调用均在服务器上执行，使用项目 venv：

```bash
# 服务器项目根目录
cd /mnt/nvme/calvin/code/deer-flow
```

---

### Phase 1 — 条文检索（`building-code-rag`）✓ 已完成

**输入**：一个自然语言问题
**输出**：相关条款列表 + Qwen3-8B 结构化回答

```bash
# 基本查询（输出 JSON 到 stdout）
building-code-rag-poc/.venv/bin/python \
  skills/public/building-code-rag/retrieve.py \
  --query "防火墙的耐火极限要求是多少？"

# 保存结果到文件
building-code-rag-poc/.venv/bin/python \
  skills/public/building-code-rag/retrieve.py \
  --query "24米高住宅疏散楼梯最小净宽" \
  --output /tmp/rag_result.json
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--query` / `-q` | 必填 | 自然语言查询 |
| `--standard` | `gb50016` | 规范代号 |
| `--top-k` | `20` | 返回条款数（强条不截断） |
| `--skip-rerank` | 关 | 跳过 Rerank，使用 RRF 排序（调试用） |
| `--output` | stdout | 结果 JSON 写入路径（可选） |

**输出结构**：

```json
{
  "query": "用户查询",
  "standard": "GB_50016-20142018",
  "retrieved_clauses_count": 20,
  "mandatory_clauses_count": 8,
  "response": {
    "answer": "自然语言回答（含免责声明）",
    "applicable_clauses": [
      {"clause": "6.1.1", "text": "...", "is_mandatory": true, "relevance": "direct"}
    ],
    "referenced_clauses": [...],
    "uncertain_aspects": [],
    "out_of_scope_warnings": []
  }
}
```

---

### Phase 2 — 项目合规检查（`compliance-check`）✓ 已完成

**输入**：项目参数自由文本描述
**输出**：项目所有适用强条的完整清单 + 逐条合规判定

```bash
# 基本用法（计划接口）
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "地上11层住宅楼，总高32米，每层850m²，地下一层车库，位于城市建成区"

# 结果写入文件
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "..." \
  --output /tmp/compliance_result.json
```

**与 Phase 1 的区别**：

| | building-code-rag | compliance-check |
|---|---|---|
| 用户输入 | 一个具体问题 | 项目参数描述 |
| 查询来源 | 用户自己提问 | 系统按维度自动展开 8-12 个查询 |
| 覆盖范围 | 问什么答什么 | 主动穷举所有适用合规维度 |
| 输出 | 相关条款 + 回答 | 全量强条清单 + 逐条判定 |

**内部编排流程**：

```
自由文本描述
  ↓ ① 参数提取（LLM）→ 建筑类型/高度/面积/用途等
  ↓ ② 查询生成   → 防火间距/防火分区/疏散/消防车道/消防设施...
  ↓ ③ 并行检索   → 多次调用 building-code-rag 检索模块
  ↓ ④ 合并去重   → 按 clause_path 去重
  ↓ ⑤ 合规判定   → 逐条：符合 / 需核实 / 需补充信息 / 不适用
  ↓ ⑥ 反思校验   → 检查是否有维度遗漏
输出：结构化合规报告
```

内部编排脚本：`scripts/08_extract_params.py`（参数提取）、`09_gen_queries.py`（查询矩阵）、`10_compliance_check.py`（端到端编排）。

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

---

## 常用命令（快速参考）

所有命令在 `building-code-rag-poc/` 目录下执行。

```bash
# 环境管理
cd building-code-rag-poc && uv add <package>   # 安装新依赖（写入 pyproject.toml）
cd building-code-rag-poc && uv sync             # 同步环境到 pyproject.toml/uv.lock

# PDF 分块解析（大文件用，如 GB 50016 共 464 页用 80 页/块）
CUDA_VISIBLE_DEVICES=2 HF_ENDPOINT=https://hf-mirror.com \
  .venv/bin/python scripts/split_and_parse.py \
  --pdf data/raw/<xxx>.pdf --chunk-size 80

# 条款树提取
.venv/bin/python scripts/02_extract_clauses.py \
  --input "data/parsed/<basename>/auto/<basename>_content_list.json" \
  --standard-id "<standard>" \
  --output-dir data/structured/

# 建双索引（BM25 + 向量）
.venv/bin/python scripts/04_build_index.py \
  --input data/structured/<standard>_clauses.json \
  --embed-url http://localhost:8097 --embed-model-id /model

# 检索验证
.venv/bin/python scripts/05_retrieve.py \
  --store-dir data/vector_store/<standard> \
  --query "<查询>" --skip-rerank
```
