# Civil Engineering Code based agents

> 基于 deer-flow（super agent harness）构建建筑领域 agents。本文档是**项目级共享上下文**，始终加载。详细需求与进度已下沉到各子项目，按需加载：

| 文件 | 内容 | 何时读 |
|---|---|---|
| `ce-code/PRD.md` | 知识层需求：算量组价造价知识底座（背景/使用场景/范围边界/收录范围/核心原则/验收标准）；实现细节已剥离至 DEV | 改 `ce-code/` 检索/数据代码前 |
| `ce-code/DEV.md` | 知识层开发：架构/流水线/存储/检索策略/质量度量/依赖服务（决策记录为主）——规范类知识（清单/计量规范）+ 结构化造价数据（定额/价格/历史） | 配环境 / 排查服务依赖时 |
| `ce-code/TODO.md` | 知识层进度 | 看知识层做到哪了 |
| `ce-services/PRD.md` | 任务层需求：目标用户、生成层、合规编排、agent 集成、任务服务端点、风险红线 | 改 `ce-services/` 生成/编排代码前 |
| `ce-services/DEV.md` | 任务层开发：依赖服务（Qwen3 + 知识服务 :8100）、env 配置 | 配环境 / 排查服务依赖时 |
| `ce-services/TODO.md` | 任务层进度 | 看任务层做到哪了 |
| `ce-bim/PRD.md` | BIM 底座层需求：为什么独立成层、GlobalId 连接键、BIM 原语端点、消费方矩阵、风险红线 | 改 `ce-bim/` 代码前 |
| `ce-bim/DEV.md` | BIM 底座层开发：依赖服务（IfcOpenShell/MinIO/:8102）、前端 viewer 选型 | 配环境 / 排查服务依赖时 |
| `ce-bim/TODO.md` | BIM 底座层进度 | 看 BIM 底座做到哪了 |
| `cost_agent_prd.md` / `cost_agent_tech.md` | 算量计价 CostAgent（BIM 消费方之一）：产品需求 / 技术方案 | 改造价算量/编排/前端代码前 |
| `backend/` | deer-flow 后端代码目录 | 涉及开发调试时 |

---

## 1. deer-flow 参考

编排入口见 `backend/CLAUDE.md`，agent 约定见 `backend/AGENTS.md`，后端代码见 `backend/`。所使用的LLM：Qwen3-8B。

---

## 2. 开发工作流与环境

### 2.1 设备分工

| 设备 | 用途 |
|---|---|
| 本地（Mac） | AI 辅助编程、代码修改、commit & push |
| 远程 Linux 服务器（有 GPU） | 运行/调试、跑 MinerU / 向量化 / 模型推理 |
| 同步通道 | GitHub（用户 fork 作中转） |

### 2.2 开发约定

- **commit 信息必须使用中文**
- 每次改完代码，**先询问用户是否 commit/push，等确认后再执行**，不得自动提交
- **本地不提交、不 push `uv.lock`**（`backend/uv.lock`、`ce-code/uv.lock`）：依赖锁文件以服务器（实际装依赖处）为准，本地 Mac 改动不入 commit。commit/push 时把 `uv.lock` 留在工作区不 `git add`
- **文档/示例里的 shell 命令一律写成单行**，不用 `\` 多行续行——多行命令复制粘贴到服务器终端时续行常被 `>` 提示符打断，导致 `Command 'run' not found` 之类的报错
- POC 代码放项目根下（`ce-code/` 知识层 + `ce-services/` 任务层 + `ce-bim/` BIM 底座层 + 未来 `ce-cost/` 算量计价，均与 `backend/` 平级），正常 commit 同步。端口约定：:8100 知识 / :8101 任务 / :8102 BIM 底座
- **BIM 是横切共享底座，不是 CostAgent 私有输入**：BIM 模型的取数底座（IfcOpenShell + IFC 原件 + 原语）落 `ce-bim/`（单一 owner，类比 `ce-code`），查看/操作做成共享前端包 `ce-bim-viewer`（web-ifc），算量计价/审图/FM 均为消费方做 HTTP 客户端复用——别把共享能力埋进单个产品
- 各层 `PRD/DEV/TODO/README` 随 git 同步到服务器（项目文档跟着代码走）；**本文档 `CLAUDE.md` 也随 git 同步**（项目级共享上下文跟着代码走，与各层文档一致）——含服务器路径/内网 IP/端口等环境细节，仅内网可达、非公网机密，可入 git/push
- 数据文件 `ce-code/data/` 下（不进 git，PDF 版权敏感）：`raw/`（PDF）、`parsed/`（MinerU 输出）、`structured/`（条款库 JSON）、`vector_store/`（BM25 + Milvus 索引）、`eval_set/`（评测集，入 git）

### 2.3 服务器环境

- 服务器ip：172.19.3.136
- 服务器路径：`/mnt/nvme/calvin/code/deer-flow/`（home: `/home/caic`）
- Python **3.12.13**，PyTorch **2.5.1+cu121**；包管理器 **uv 0.11.14**，依赖统一用 `uv add`，**严禁 `uv pip install`** 绕过 `pyproject.toml`
- GPU：4x RTX 4090（各 24564 MiB），驱动 535.230.02
