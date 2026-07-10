# Civil Engineering Code based agents

> 基于 deer-flow（super agent harness）构建建筑领域 agents。本文档是**项目级共享上下文**，始终加载。详细需求与进度已下沉到各子项目，按需加载：

| 文件 | 内容 | 何时读 |
|---|---|---|
| `AGENT_TODO.md` | **主线总控**：当前阶段（benchmark 测试-优化-迭代）、M0~M3 批次任务、小尾巴、挂起决策 | 看整体做到哪了 / 定下一步做什么 |
| `ce-code/PRD.md` | 组价知识库需求：深圳房建组价造价底座（背景/使用场景/范围边界/收录范围/核心原则/验收标准）；实现细节剥离至 DEV | 改 `ce-code/` 数据/取数代码前 |
| `ce-code/DEV.md` | 组价知识库开发：架构 ingest→cost→PG→取数 / 存储（PG + spec 版本隔离 + Milvus 清单库）/ 取数策略 / 质量度量 / 依赖服务（决策记录为主） | 配环境 / 排查服务依赖时 |
| `ce-code/TODO.md` | 组价知识库进度 | 看 ce-code 做到哪了 |
| `backend/` | deer-flow 后端代码目录（组价编排已内嵌于此：`backend/app/ce/cost/` 的 `cost_workflow_*`） | 涉及开发调试时 |
| `docker/README.md` | **全服务 + 依赖的启动手册**（拓扑 + prod/dev 两态启动 + 登录/健康/停更，单一入口） | 起服务 / 排查启动依赖时 |

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
- **改动 deerflow 后端源码（`backend/` 目录）的 commit，消息开头必须加 `[backend]` 标注**（如 `[backend] feat: ...`）：deerflow 是上游 super-agent harness，对它的修改需与项目自有代码（`ce-*`）在提交历史里一眼可分，便于日后向上游回流/对账。一个 commit 同时动了 `backend/` 和 `ce-*` 时也加 `[backend]`（只要碰了后端源码就标）；纯 `ce-*`/文档改动不加
- 每次改完代码，**先询问用户是否 commit/push，等确认后再执行**，不得自动提交
- **本地不提交、不 push `uv.lock`**（`backend/uv.lock`、`ce-code/uv.lock`）：依赖锁文件以服务器（实际装依赖处）为准，本地 Mac 改动不入 commit。commit/push 时把 `uv.lock` 留在工作区不 `git add`
- **给用户的任何终端命令一律写成单行**（不只文档/示例，也包括对话里直接贴给用户去服务器执行的命令）：不用 `\` 多行续行，不用 `<<EOF` 多行 heredoc，不用跨行的 `for/if/while` 块——多行内容复制粘贴到服务器终端时续行常被 `>` 提示符打断，导致 `Command 'run' not found` 之类报错。需要多步就拆成多条独立单行命令，或用 `&&`/`;` 串成一行；需要多行文件内容时改用「写好文件再执行」而非 heredoc 贴命令
- POC 代码放项目根下（`ce-code/` 知识层，与 `backend/` 平级），正常 commit 同步。端口约定：ce-rag :8100 检索 / ce-db :8102 结构化真值（均为 MCP 服务，供 backend agent 消费）。**原 `ce-services/` 任务层（:8101）已整体退役**——组价编排/规范问答已内嵌进 backend（`cost_workflow_*` + norm-qa/cost-agent 子智能体 + ce-rag/ce-db MCP）；其唯一遗留的选码评测引擎已迁至 `benchmark/select_eval/`
- 各层 `PRD/DEV/TODO/README` 随 git 同步到服务器（项目文档跟着代码走）；**本文档 `CLAUDE.md` 也随 git 同步**（项目级共享上下文跟着代码走，与各层文档一致）——含服务器路径/内网 IP/端口等环境细节，仅内网可达、非公网机密，可入 git/push
- 数据文件 `ce-code/data/`：**入 git 同步** `parsed/`（MinerU 输出）、`structured/`（chunk 树 / bill_spec.jsonl 等结构化产物）——派生数据走 git 在 Mac/服务器间同步（2026-06-16 起）；**仍不进 git** `raw/`（PDF 原件，版权敏感）、`vector_store/`（BM25 + Milvus 索引，大体积二进制、可重新生成）、`eval_set/`（仅余评测 xlsx 原件）。**评测金标集已迁项目根 `benchmark/`**（`routing_eval/` 路由评测 + `retrieval_eval/` 清单匹配/条文召回金标，随 git 同步）
- **作为开发者，禁止用「前端调大模型对话生成」的方式新建 skill 或 agent**（即 `/workspace/agents/new` 的 bootstrap 流程、`/workspace/chats/new?mode=skill` 的 skill-creator 流程）：① 产物落 `.deer-flow/users/{user}/agents/` 与 `skills/custom/`，均**不随 git 同步**、按机器隔离，与 Mac↔服务器 git 工作流不一致；② **这些前端自助创建功能未来会被删除**。开发态的 skill 一律**手写进 `skills/public/{name}/SKILL.md`（随 git 同步）**，agent 定义同理走代码/git 纳管，不依赖前端生成

### 2.3 服务器验证两态：dev 调试态 / Docker 生产态（2026-07-06 定）

- **日常改码验证一律用 dev 调试态，不重建 Docker**：backend 用 VS Code debugpy 起 uvicorn :8001，
  launch.json **必须带 `"envFile": "${workspaceFolder}/.env"`**——LANGFUSE / 模型密钥等运行时开关都在
  `.env` 里，缺了 envFile 等于拿残缺环境验、验了等于白验；frontend
  `pnpm exec next dev --turbo --port 2026`（next.config 的 dev rewrites 直连 :8001 无需 nginx，内网直访
  172.19.3.136:2026 已放行）。改后端 F5 重启调试（秒级），改前端热更新。**debugpy 下禁加 `--reload`**
  （fork 子进程断点脱靶）。
- 起 dev 前先停 Docker harness 层腾 :2026：`sudo ./scripts/deploy.sh down`（ce-code/精排/监控
  容器**不动**，dev 后端还要调它们）。账号/线程数据共用 `backend/.deer-flow` 两态无缝；前端命令带
  `BETTER_AUTH_SECRET=$(cat ../backend/.deer-flow/.better-auth-secret)` 保登录态。
- **两态唯一要来回翻的配置**：`config.yaml` 的 vLLM base_url（Docker 态 `host.docker.internal:8099` ↔
  dev 态 `localhost:8099`）；后端连不上 8099 先查这里。
- **benchmark runner 与两态无关**：走 DeerFlowClient 嵌入式、进程内加载源码，`git pull` 即新代码，
  不需要起 gateway。
- **dev 态验不了的四边界**（批次收尾必须过一次生产态）：① 前端类型错误——next dev（Turbopack）不跑
  tsc，动过 `.ts/.tsx` 回生产前先 `pnpm typecheck`；② 多 worker 行为（生产 `--workers 4`，dev 单进程）；
  ③ Dockerfile/compose/nginx/容器网络类改动；④ `uv add` 新依赖须重建镜像才进容器。
- **切回生产态**（每批次一次，不逐次改逐次建）：sed 翻回 host.docker.internal → `sudo ./scripts/deploy.sh`；
  动过后端源码须防 COPY 层缓存坑：`build --no-cache gateway` + `up -d --force-recreate gateway`（详见
  `docker/README.md` §7）；动 harness/ce-code 容器一律 `sudo`（rootless 会建出影子容器）。

### 2.4 服务器环境

- 服务器ip：172.19.3.136
- 服务器路径：`/mnt/nvme/calvin/code/deer-flow/`（home: `/home/caic`）
- Python **3.12.13**，PyTorch **2.5.1+cu121**；包管理器 **uv 0.11.14**，依赖统一用 `uv add`，**严禁 `uv pip install`** 绕过 `pyproject.toml`
- GPU：4x RTX 4090（各 24564 MiB），驱动 535.230.02
