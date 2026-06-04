# ce-code（知识层）· 开发文档

> 知识层开发的**依赖服务与环境**。需求/设计见 `PRD.md`，进度见 `TODO.md`，操作命令（流水线/起服务）见 `README.md`，项目级共享约定（git/设备分工）见根 `CLAUDE.md`。

---

## 依赖服务（服务器已部署）

知识层 = 数据 + 检索，用到以下服务：

| 角色 | 模型 | 地址 | 知识层用途 | 备注 |
|---|---|---|---|---|
| Embedding | bge-large-zh-v1.5 | `http://localhost:8097`，model_id `/model` | 条款向量化、query embedding | dim=1024，max_len=512 |
| 向量库 | Milvus | `http://localhost:19530` | 向量存储与检索 | MilvusClient API；collection 名只含字母/数字/下划线 |
| rerank | bge-reranker-large | 本地 FlagEmbedding | 检索结果重排（强条不截断） | 不可用时自动 RRF fallback |
| VLM | Qwen2.5-VL-7B | `http://localhost:8098`，model_id `/model` | PDF 解析时图示理解 | — |
| 文本生成 / 推理 | Qwen3-8B | `http://localhost:8099`，model_id `qwen3-8b` | 查询改写（生成 3-5 变体）、引用图/条款树 LLM 校验 | `/think` 启用 thinking、`/no_think` 禁用；JSON 输出建议 `/no_think` |

> 生成（问答）/合规编排不在知识层 —— 那是任务层（`../ce-services/`）的事，它用 Qwen3-8B 做生成/判定。

### 造价轨（CostAgent / 算量组价 agent）新增依赖（待部署，对应 PRD §5 三层知识底座）

| 角色 | 选型 | 知识层用途 | 备注 |
|---|---|---|---|
| 关系库 | PostgreSQL | 规范(GB50500/50854)/定额/价格/历史的强一致精确查询，version + region 维度 | 单一事实来源；JSONB 存 feature_schema/适用范围 |
| 知识图谱 | Neo4j | 构件→清单→定额→工料机多跳关系（组价核心） | **P0 先用 PG 关联表模拟，P1 再上 Neo4j** |
| Embedding（造价） | BGE-M3 | 清单规范条文/做法/历史案例向量化（dense+sparse 混检） | 与规范轨 bge-large-zh-v1.5 是否合并待评估 |
| 向量库 | Milvus | 造价 `bill_spec_kb` collection（复用规范轨同一 Milvus 实例 :19530） | collection 名只含字母/数字/下划线 |

> 算量引擎（几何 + 扣减）、图纸解析（IFC/DXF/PDF：IfcOpenShell/ezdxf/PyMuPDF）、对象存储（MinIO 图纸/产物）属**任务层**，不在知识层依赖范围。

### 造价数据构建管线（PRD §5.2，对应 `cost_agent_tech.md` §3.4）

```
GB 计价/计量规范 PDF ──MinerU + 规则──> bill_spec 表
定额电子表 ──────────导入/清洗────────> quota_item + quota_resource + resource
信息价文件 ──────────定期抓取/导入────> resource_price（带 effective_period 时效）
历史项目 ────────────归档 + 脱敏──────> hist_bill
上述各库 ────────────实体/关系抽取────> Neo4j KG（P0 用 PG 关联表）
规范/案例 ───────────切分 + BGE-M3────> Milvus bill_spec_kb
```

---

## 开发环境要点（知识层专属）

- **GPU 选择**：MinerU 解析用 `CUDA_VISIBLE_DEVICES=2`（GPU 2 空闲显存最多 ~17 GB；GPU 1/3 被 vLLM 占用，GPU 0 偏紧）
- **模型下载**：需设 `HF_ENDPOINT=https://hf-mirror.com`（服务器默认无法直连 HuggingFace）
- **关键依赖版本约束**：
  - MinerU **3.2.0**（装入项目 venv，✓ 已验证）
  - mineru-vl-utils **1.0.2**，依赖 `transformers>=4.51.1,<5.0.0`（**不可升 5.x**，否则 Qwen2VLConfig 不兼容）
  - pymilvus **3.0.0**（MilvusClient API；ORM-style 已弃用）；rank-bm25 ✓
  - PyTorch **2.5.1+cu121**（`pyproject.toml` 已配 pytorch-cu121 uv index；mineru 声明需 >=2.6.0 但实测可用，`[tool.uv] override-dependencies` 绕过）
- **包管理**：`uv add` 管理依赖，**严禁 `uv pip install`** 绕过 `pyproject.toml`

> 共享环境基础（服务器路径、Python 版本、uv 版本、GPU 硬件）见根 `CLAUDE.md` §2.3。

---

## 起服务 / 跑流水线

知识服务（:8100）启动、PDF 解析 → 条款树 → 建索引的完整流水线命令见 `README.md`。
