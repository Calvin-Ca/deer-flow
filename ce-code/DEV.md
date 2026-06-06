# ce-code（知识层）· 开发文档

> 知识层开发的**依赖服务与环境**。需求/设计见 `PRD.md`，进度见 `TODO.md`，操作命令（流水线/起服务）见 `README.md`，项目级共享约定（git/设备分工）见根 `CLAUDE.md`。

---

## 依赖服务（服务器已部署）

知识层 = 数据 + 检索，用到以下服务：

| 角色 | 模型 | 地址 | 知识层用途 | 备注 |
|---|---|---|---|---|
| Embedding | bge-large-zh-v1.5 | `http://localhost:8097`，model_id `/model` | 条款向量化、query embedding | dim=1024，max_len=512 |
| 向量库 | Milvus | `http://localhost:19530` | 向量存储与检索 | MilvusClient API；collection 名只含字母/数字/下划线 |
| VLM | Qwen2.5-VL-7B | `http://localhost:8098`，model_id `/model` | PDF 解析时图示理解 | — |
| 文本生成 / 推理 | Qwen3-8B | `http://localhost:8099`，model_id `qwen3-8b` | 查询改写（生成 3-5 变体）、引用图/条款树 LLM 校验 | `/think` 启用 thinking、`/no_think` 禁用；JSON 输出建议 `/no_think` |

> 生成（问答）/合规编排不在知识层 —— 那是任务层（`../ce-services/`）的事，它用 Qwen3-8B 做生成/判定。

**依赖健康自检（排查"起不来"先逐个确认依赖活着，命令单行）：**

- Embedding：`curl -s http://localhost:8097/v1/models`
- VLM：`curl -s http://localhost:8098/v1/models`
- Qwen3-8B：`curl -s http://localhost:8099/v1/models`
- Milvus：`curl -s http://localhost:9091/healthz`（默认 metrics/health 端口；19530 为 gRPC）
- 知识服务自身：`curl -s http://localhost:8100/health`（含 ready_standards / vector_store / deps 地址）

### 造价轨（CostAgent / 算量组价 agent）新增依赖（待部署）

> 三层知识底座的**设计**（职责分工、数据资产、KG schema、构建管线）见 `PRD.md §5`；本表只列**依赖服务的选型/地址/约束**。

| 角色 | 选型 | 地址 | 备注 |
|---|---|---|---|
| 关系库 | PostgreSQL | 待部署 | 单一事实来源；JSONB 存 feature_schema/适用范围 |
| 知识图谱 | Neo4j | 待部署 | **P0 先用 PG 关联表模拟，P1 再上 Neo4j** |
| Embedding（造价） | BGE-M3 | 待部署 | dense+sparse 混检；与规范轨是否合并为单服务待评估 |
| 向量库 | Milvus | `http://localhost:19530` | 造价 `bill_spec_kb` collection，复用规范轨同一实例 |

> 算量引擎（几何 + 扣减）、图纸解析（IFC/DXF/PDF：IfcOpenShell/ezdxf/PyMuPDF）、对象存储（MinIO 图纸/产物）属**任务层**，不在知识层依赖范围。

---

## 开发环境要点

- **GPU 选择**：MinerU 解析用 `CUDA_VISIBLE_DEVICES=2`（GPU 2 空闲显存最多 ~17 GB；GPU 1/3 被 vLLM 占用，GPU 0 偏紧）
- **模型下载**：需设 `HF_ENDPOINT=https://hf-mirror.com`（服务器默认无法直连 HuggingFace）
- **关键依赖版本约束**：
  - MinerU **3.2.0**（装入项目 venv，✓ 已验证）；另有一台远程 MinerU API 主机 `172.19.2.2:8000` 版本 **3.2.1**（两者解析方式对比见下节）
  - mineru-vl-utils **1.0.2**，依赖 `transformers>=4.51.1,<5.0.0`（**不可升 5.x**，否则 Qwen2VLConfig 不兼容）
  - pymilvus **3.0.0**（MilvusClient API；ORM-style 已弃用）；rank-bm25 ✓
  - PyTorch **2.5.1+cu121**（`pyproject.toml` 已配 pytorch-cu121 uv index；mineru 声明需 >=2.6.0 但实测可用，`[tool.uv] override-dependencies` 绕过）

> 共享环境基础（服务器路径、Python 版本、uv 版本、GPU 硬件）见根 `CLAUDE.md` §2.3。

---

## MinerU 两种解析方式

PDF 解析有两条路径，**本质是同一套 MinerU 代码**（CLI 运行时会临时起一个本地 mineru-api 提交任务，启动日志可见 `Started local mineru-api at http://127.0.0.1:...`，跑完即关），差异只在「冷热」和「环境」。实测同 backend 同输入下，两者产出的 md **md5 逐字一致**（pipeline / 第300页 / 即使 CLI 3.2.0 vs API 3.2.1 也无差异）。

**管线默认走远程 API**：`pipeline/01_parse_pdf.py` 默认调 API（封装在 `pipeline/mineru_api.py`，请求 `response_format_zip=true` 拿到标准 `auto/` 布局 ZIP 解压落盘），加 `--local` 才用本地 CLI。原因：API 主机 vllm 正常、`hybrid-auto-engine` 现成可用，且常驻热服务无冷启动；本地 venv 的 vllm 当前损坏，CLI 跑不了 hybrid。

| | 远程 API（**默认**） | 本地 CLI（`--local`） |
|---|---|---|
| 入口 | `01_parse_pdf.py`（默认）→ `POST http://172.19.2.2:8000/file_parse` | `01_parse_pdf.py --local` / `split_and_parse.py`（大 PDF 分块）→ `mineru -p ... -o ...` |
| 版本 | 3.2.1 | 3.2.0 |
| 速度 | 常驻热服务，单页 ~1.8s | 冷启动每次 ~23s 模型加载 + 起关 server，单页约 1 分钟 |
| hybrid 后端 | ✅ 可用（该主机 vllm 正常） | ❌ 当前 venv 的 vllm ABI 损坏（见下），跑不了 |
| 文件传输 | 每次调用都重传整个 PDF（分批解析会重复上传） | 读盘一次 |
| 适用场景 | **默认全场景**（在线单文件 + 批量建库，hybrid 现成、零本地依赖） | 离线 / 省带宽大批量；大 PDF 用 `split_and_parse.py` 分块避免本地 OOM |

### backend 选择：定额/造价类表格必须用 `hybrid-auto-engine`

- `pipeline`（不依赖 vllm）：通用、多语言、无幻觉，但**密集多列定额表会列错位**（colspan/rowspan 对齐失败，数字串列）
- `hybrid-auto-engine`（需 vllm）：表格结构、人材机编码/单位/单价/消耗量逐列对位，实测明显优于 pipeline
- CLI 默认走 pipeline；批量建库前需显式加 `-b hybrid-auto-engine -t true`（`01_parse_pdf.py` 当前未指定 backend，待修）

### ⚠️ venv 的 vllm 当前损坏（hybrid 前置阻塞）

```
vllm/_C.abi3.so: undefined symbol: _ZN3c106ivalue14ConstantString6create...
```

`_ZN3c10...` 是 PyTorch c10 符号 —— vllm 编译时链接的 libtorch 与当前 PyTorch 2.5.1+cu121 ABI 不匹配（vllm 与 torch 版本对不上）。`hybrid-auto-engine` 的 VLM 部分依赖 `vllm-async-engine`，vllm 一坏整个 hybrid 即 fail。**在本地 CLI 用 hybrid 建库前，必须先把 vllm pin 到与 torch 2.5.1 匹配的版本重装（用 `uv add`，勿 `uv pip install`）。**

---