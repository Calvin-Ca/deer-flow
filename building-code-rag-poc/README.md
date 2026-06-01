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
└── service/
    └── server.py                   # 常驻 HTTP 服务（FastAPI，端口 8100）：
                                    #   /retrieve（包 05+06）、/compliance（包 10）
                                    #   skill 侧用标准库 HTTP 调用，沙箱内零依赖
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

POC 脚本跑通后，检索与合规判定功能封装为 deer-flow skills。

**架构（重要）**：skill 不再直接加载 POC 脚本，而是通过 HTTP 调用一个**常驻服务**
（`service/server.py`，端口 8100）。这样 skill 在 deer-flow 沙箱内**零第三方依赖**
（只用标准库 urllib），不依赖 venv / 向量索引数据 / POC 脚本——与 deer-flow 只把
`skills/` 挂进沙箱的机制契合。

**先在服务器上启动常驻服务**（一次性）：

```bash
cd /mnt/nvme/calvin/code/deer-flow/building-code-rag-poc
.venv/bin/python service/server.py            # 监听 0.0.0.0:8100
# 或： .venv/bin/python -m uvicorn service.server:app --host 0.0.0.0 --port 8100
curl http://localhost:8100/health             # 检查就绪状态 + 已建索引的规范
```

---

### Phase 1 — 条文检索（`building-code-rag`）✓ 已完成

**输入**：一个自然语言问题
**输出**：相关条款列表 + Qwen3-8B 结构化回答

```bash
# 基本查询（用系统 python3，无需 venv；服务跑在 8100）
python3 skills/public/building-code-rag/retrieve.py \
  --query "防火墙的耐火极限要求是多少？"

# 保存结果到文件
python3 skills/public/building-code-rag/retrieve.py \
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
# 基本用法（用系统 python3；服务跑在 8100）
python3 skills/public/compliance-check/check.py \
  --project "地上11层住宅楼，总高32米，每层850m²，地下一层车库，位于城市建成区"

# 结果写入文件
python3 skills/public/compliance-check/check.py \
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

# 启动常驻 HTTP 服务（skill 走 HTTP 调用，端口 8100）
.venv/bin/python service/server.py
curl http://localhost:8100/health
```

---

## 知识层设计（目标架构，v2）

> 阶段 1/2 已落地（条款树 + 混合检索 + 合规编排）。本节是面向阶段 3 与多规范扩展的**知识层再设计目标**，尚未全部落地，作为重构方向记录。三个目标 agent（规范问答 / 算量组价 / 图纸审核）共享同一知识层，差异只在输入适配与计算逻辑。

### 核心判断

知识层的主资产**不是 embedding，是"条款的结构化表征"**。建筑规范两条铁律——*漏强条 = 合规事故*、*适用性判断是一切*——决定召回不能赌语义相似：**引用图 与 适用范围索引 的权重高于向量索引**，embedding 只是最不可靠的一个召回入口。

知识层 = **多表征条款库**，同一批条款四种并存表征，检索是四者的可组合并集：

| 表征 | 载体 | 召回作用 | 主要场景 |
|---|---|---|---|
| ① 文本 | 向量 + BM25 | 语义 / 条文号·术语精确 | 问答 |
| ② 树 | 章→节→条→款→项 | 层级上下文 | 所有场景 |
| ③ 引用图 | references（分型+双向） | 引用扩展（强条不漏） | 所有场景 |
| ④ 条件 | 结构化适用范围谓词 | 条件匹配召回 | 算量 / 审图 / 合规 |

### 资产建模要点（按优先级）

- **【P0】黑体强条 ≠ 语气强制**：拆 `modal_strength`（必须/严禁/应/宜/可）与 `is_mandatory_clause`（规范正式黑体字标注）。"漏强条=事故"特指漏黑体强条；解析须保留 MinerU 版面分析的字重信息。
- **【P0】引用边分型 + 双向**：`strong`（应符合→必拉、继承强制性）/ `weak`（参见→可选）/ `exclude`（本条不适用→禁止正向扩展）/ `cross_standard`（触发多规范召回）。自动引用扩展只对 `strong` 执行；存 `referenced_by` 反向边。
- **【P0】表格可查询**：强制要求大量在表格里（防火间距/耐火极限/疏散宽度）。表格 → 结构化 JSON，支持"给定行列条件取值"，继承条款强制性，不能只当图片。
- **【P1】适用范围谓词抽取**：散文条件（"建筑高度大于 54m 的住宅…"）抽成结构化谓词，是算量/审图的桥，工程量最大的天花板。抽不准标 `scope_status: unknown` 进保守召回。
- **【P1】chunk 携带祖先链**：叶子款/项向量化拼上祖先标题链 + 所属"条"全文；召回既给命中款也给完整条。
- **【P2】条款级版本/效力**：`status`/`version`/`effective_date` 到条款粒度；废止条款不召回但保留。多规范从一开始就支持（GB 50116 待收录）。

精化后的条款 schema 见仓库根 `CLAUDE.md` §4.1（本地）。

### 检索面（四通道）

```
Query → ① 向量(语义) + ① BM25(条文号/术语) + ④ 适用范围结构化过滤
      → 合并去重 → ③ 引用图扩展(仅 strong 边) → rerank(强条不截断)
      → 返回(命中款 + 完整条 + 强制性 + 引用)
※ ④ 适用范围过滤优先于排序：先按 standard/version/scope 圈定范围，再排序。
```

### 服务 / skill 拓扑：一个知识服务，N 个 skill

知识层是独立常驻服务（非 skill），skill 只是每个 agent 的薄适配器。当前 `service/server.py` 只暴露两个**焊死的任务端点**（`/retrieve`=检索+生成、`/compliance`=整条流水线），检索原语被埋在 05/06/10 脚本里——这是最大扩展性债务：算量/审图 agent 落地时无法复用"裸检索 / 引用扩展 / 范围过滤"。目标：

- **服务层双层 API**：原语端点（`/search` `/expand` `/filter` `/rerank` `/clause/{path}`）+ 任务端点（`/qa` `/compliance`）。
- **检索与生成解耦**：`/retrieve` 拆成 `/search`（裸条款）+ `/qa`（=search+generate）；算量/审图要裸条款。
- **引擎毕业成 package**：05/06/10 逻辑收敛成 `bcrag/` 包，服务与脚本都 import 它，不再 `importlib` 按文件名加载编号脚本。
- **接入层映射**：`building-code-rag`→`/qa`、`compliance-check`→`/compliance`、〔算量〕→`/search`+`/clause`→自有 sandbox、〔审图〕→解析图纸→`/search`+`/filter`→比对。
- **HTTP vs MCP**：现 skill→HTTP 本质是手写版 MCP；第二个 agent（算量）落地、原语集稳定后再考虑迁 MCP。

**重构优先级**：先还 P0 = 暴露原语 + 检索/生成解耦（算量 agent 一落地就撞），随后并行打磨黑体强条标注、引用边分型、表格结构化、适用范围谓词抽取。
