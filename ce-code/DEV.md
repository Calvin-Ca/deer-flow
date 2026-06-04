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
