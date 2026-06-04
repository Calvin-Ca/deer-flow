# ce-services（任务层）· 开发文档

> 任务层开发的**依赖服务与环境**。需求/设计见 `PRD.md`，进度见 `TODO.md`，操作命令（起服务）见 `README.md`，项目级共享约定（git/设备分工）见根 `CLAUDE.md`。

---

## 依赖服务

任务层 = 生成 + 编排，是知识服务的纯 HTTP 客户端，只用到：

| 角色 | 模型 / 服务 | 地址 | 任务层用途 | 备注 |
|---|---|---|---|---|
| 文本生成 / 推理 | Qwen3-8B | `http://localhost:8099`，model_id `qwen3-8b` | 问答生成、合规判定、反思校验、参数提取 | `/think` 启用 thinking、`/no_think` 禁用；JSON 输出建议 `/no_think` |
| 检索（内部依赖） | 知识服务 | `http://localhost:8100` | 打 `/search` 拿裸条款 | 由 `common/knowledge_client.py` 封装；必须先起 |

> 任务层**不直连** Embedding / Milvus / VLM —— 那些是知识层（`../ce-code/`）的资产，任务层一概不碰。

---

## 配置（env 覆盖，见 `common/config.py`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BCRAG_KNOWLEDGE_URL` | `http://localhost:8100` | 知识服务地址 |
| `BCRAG_LLM_URL` | `http://localhost:8099` | Qwen3-8B vLLM 地址 |
| `BCRAG_LLM_MODEL_ID` | `qwen3-8b` | LLM model_id |

---

## 开发环境要点（任务层专属）

- **独立 uv 项目**：依赖极轻（仅 `fastapi`/`uvicorn`/`requests`/`pydantic`），首次 `cd ce-services && uv sync`
- **不依赖 GPU / torch / Milvus 客户端**：镜像可极轻量（Docker tasks 镜像 ~200MB）
- **包管理**：`uv add` 管理依赖，**严禁 `uv pip install`** 绕过 `pyproject.toml`

> 共享环境基础（服务器路径、Python 版本、uv 版本）见根 `CLAUDE.md` §2.3。

---

## 起服务

任务服务（:8101，`/qa` + `/compliance` 共进程）启动命令（含 Docker 全栈）见 `README.md`。**前置：知识服务 :8100 必须先起。**
