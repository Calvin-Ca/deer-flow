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

## 1. Agent 能力需求（做什么）

> **用户画像：深圳地区房建专业、使用 2013 版规范的造价从业者**（2026-07-11 定）。这是产品与评测的共同基准——评测用例按此画像造。
>
> **Agent 定位：深圳市房建专业智能组价助手，口径深圳·2013 版规范——系统唯一支持的口径**（2026-07-11 裁定，**2024 版已裁出产品范围**）：版本、地区**一律不反问**，直接按深圳·2013 执行并在回复中声明；用户点名 2024 版或他省口径 → 不取数、不拿 2013 数据冒充作答，体面告知仅支持深圳·2013。6 类核心能力如下，**均可触发 human-in-the-loop**——**实质信息**不足时（构件特征/清单内容/计算参数，非版本地区）`ask_clarification` 向用户追问，关键结论落定前请人确认（`ClarificationMiddleware` 中断等人）。

1. **规范问答助手**：回答清单计价规范、定额规范（含工程量计算规则等）、信息价相关的问题。
2. **清单智能匹配**：给定项目特征，匹配清单编码；给定清单编码及项目特征，核实项目特征项是否完整（有无遗漏）。
3. **定额方案推荐**：针对已编制好的清单项，推荐匹配的定额组价方案。
4. **智能询价**：回应材料询价请求，按材料/规格/期号取深圳信息价、多期走势对比；口径严格锁深圳（他省体面告知不取数）。
5. **智能计算**：当用户提出涉及组价过程任何环节（工程量、含量、单价、合价等）的计算要求时，能智能匹配计算规则自动完成计算。
6. **整单组价全闭环**：给定项目清单，串起「清单智能匹配 → 定额方案推荐 → 组价自动计算」，帮用户完成整单组价的端到端闭环（编排能力 2/3/5）。

---

## 2. deer-flow 参考

编排入口见 `backend/CLAUDE.md`，agent 约定见 `backend/AGENTS.md`，后端代码见 `backend/`。所使用的LLM：Qwen3-8B。

---

## 3. 开发工作流与环境

### 3.1 设备分工

| 设备 | 用途 |
|---|---|
| 本地（Mac） | AI 辅助编程、代码修改、commit & push |
| 远程 Linux 服务器（有 GPU） | 运行/调试、跑 MinerU / 向量化 / 模型推理 |
| 同步通道 | GitHub（用户 fork 作中转） |

### 3.2 开发约定

- **commit 信息必须使用中文**
- **改动 deerflow 后端源码（`backend/` 目录）的 commit，消息开头必须加 `[backend]` 标注**（如 `[backend] feat: ...`）：deerflow 是上游 super-agent harness，对它的修改需与项目自有代码（`ce-*`）在提交历史里一眼可分，便于日后向上游回流/对账。一个 commit 同时动了 `backend/` 和 `ce-*` 时也加 `[backend]`（只要碰了后端源码就标）；纯 `ce-*`/文档改动不加
- 每次改完代码，**先询问用户是否 commit/push，等确认后再执行**，不得自动提交
- **本地不提交、不 push `uv.lock`**（`backend/uv.lock`、`ce-code/uv.lock`）：依赖锁文件以服务器（实际装依赖处）为准，本地 Mac 改动不入 commit。commit/push 时把 `uv.lock` 留在工作区不 `git add`
- **给用户的任何终端命令一律写成单行**（不只文档/示例，也包括对话里直接贴给用户去服务器执行的命令）：不用 `\` 多行续行，不用 `<<EOF` 多行 heredoc，不用跨行的 `for/if/while` 块——多行内容复制粘贴到服务器终端时续行常被 `>` 提示符打断，导致 `Command 'run' not found` 之类报错。需要多步就拆成多条独立单行命令，或用 `&&`/`;` 串成一行；需要多行文件内容时改用「写好文件再执行」而非 heredoc 贴命令
- POC 代码放项目根下（`ce-code/` 知识层，与 `backend/` 平级），正常 commit 同步。端口约定：ce-rag :8100 检索 / ce-db :8102 结构化真值（均为 MCP 服务，供 backend agent 消费）。**原 `ce-services/` 任务层（:8101）已整体退役**——组价编排/规范问答已内嵌进 backend（`cost_workflow_*` + norm-qa/cost-agent 子智能体 + ce-rag/ce-db MCP）；其唯一遗留的选码评测引擎已迁至 `benchmark/L2_gating/select_eval/`
- 各层 `PRD/DEV/TODO/README` 随 git 同步到服务器（项目文档跟着代码走）；**本文档 `CLAUDE.md` 也随 git 同步**（项目级共享上下文跟着代码走，与各层文档一致）——含服务器路径/内网 IP/端口等环境细节，仅内网可达、非公网机密，可入 git/push
- 数据文件 `ce-code/data/`：**入 git 同步** `parsed/`（MinerU 输出）、`structured/`（chunk 树 / bill_spec.jsonl 等结构化产物）——派生数据走 git 在 Mac/服务器间同步（2026-06-16 起）；**仍不进 git** `raw/`（PDF 原件，版权敏感）、`vector_store/`（BM25 + Milvus 索引，大体积二进制、可重新生成）、`eval_set/`（仅余评测 xlsx 原件）。**评测金标集已迁项目根 `benchmark/`**（按层分目录：`L1_routing/data/` 路由评测 + `L3_retrieval/data/` 清单匹配/条文召回金标，随 git 同步）
- **作为开发者，禁止用「前端调大模型对话生成」的方式新建 skill 或 agent**（即 `/workspace/agents/new` 的 bootstrap 流程、`/workspace/chats/new?mode=skill` 的 skill-creator 流程）：① 产物落 `.deer-flow/users/{user}/agents/` 与 `skills/custom/`，均**不随 git 同步**、按机器隔离，与 Mac↔服务器 git 工作流不一致；② **这些前端自助创建功能未来会被删除**。开发态的 skill 一律**手写进 `skills/public/{name}/SKILL.md`（随 git 同步）**，agent 定义同理走代码/git 纳管，不依赖前端生成

### 3.3 服务器验证两态：dev 调试态 / Docker 生产态（2026-07-06 定）

- **首选：`backend/debug.py` 命令行交互调试（agent 行为/路由/提示词类改动都先走这个，不起前端/gateway）**：
  进程内直建 lead agent 的 REPL——VS Code F5 启动后在集成终端里对话，执行链（中间件→模型→工具）
  全在同一进程，任意源码断点直接停。launch.json 配置要点（`type: debugpy` / `program: backend/debug.py` /
  `cwd: backend` / `python: backend/.venv/bin/python` / `env: {PYTHONPATH: "."}` / envFile 同 uvicorn 配置）；
  **`"console": "integratedTerminal"` 必须有**——`You:` 提示符要读 stdin，Debug Console 喂不了输入。
  日志落 `backend/debug.log`（终端保持干净）。trace 照常上报 Langfuse（与三方案分工/顺序见 §4.1）。
  **边界**：`create_agent` 无 checkpointer → 每条输入都是
  全新单轮对话，**多轮记忆/HITL 续跑（clarify 后答复）测不了**——那些走 `probe_gateway.py` 或前端。
  config 块已固化 `subagent_enabled: True`（缺了 task 不绑定、子智能体路由假阴性）+ 模型走 config.yaml
  默认（qwen3-8b）。
- **改到 gateway/API/前端联动时才起 dev 全栈（日常验证两级都不重建 Docker）**：backend 用 VS Code debugpy 起 uvicorn :8001，
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

### 3.4 服务器环境

- 服务器ip：172.19.3.136
- 服务器路径：`/mnt/nvme/calvin/code/deer-flow/`（home: `/home/caic`）
- Python **3.12.13**，PyTorch **2.5.1+cu121**；包管理器 **uv 0.11.14**，依赖统一用 `uv add`，**严禁 `uv pip install`** 绕过 `pyproject.toml`
- GPU：4x RTX 4090（各 24564 MiB），驱动 535.230.02

---

## 4. Benchmark 测试进度与续测入口（2026-07-10）

> 当前阶段：**路由层（L1）评测调试中**。规范/门线见 `benchmark/AGENT_BENCHMARK.md`，目录地图（层↔目录 + 命令速查）见 `benchmark/README.md`，runner 操作见 `benchmark/_shared/README.md`。**2026-07-11 起 benchmark 按层分目录**（`L1_routing/`…`L7_nfr/` + `_shared/` 基建 + `component_eval/` 零件级）。

### 4.1 三个测试方案（都在服务器跑；标准闭环：**①F5 快验 → ②runner 出分 → ③归因复现**）

改动（提示词/路由/工具）后的使用顺序：先 **① F5 debug** 手动过几条典型 query（秒级迭代，行为不对不必浪费一轮批量评测）→ 行为符合预期后 **② runner** 批量出两率、比 variant → 有失败用例先翻 **Langfuse trace 的 comment/工具链**归因，需要单条复现或涉及 clarify 续跑时才动 **③ probe**。

1. **F5 交互调试 `backend/debug.py`**（VS Code F5，进程内直建 lead agent，配置细节见 §3.3）——**日常首选**：改提示词/路由/中间件后的快速行为验证，任意源码断点同进程直接停。trace **照常上报 Langfuse**（`make_lead_agent` 在图根部挂 CallbackHandler，`load_dotenv` 读到 LANGFUSE 开关），看板 trace 树的 tool span 即工具调用序列，肉眼对 `ROUTE_TOOL_NAMES`/`ask_clarification` 即 did_route/did_clarify。**边界**：a) 无 checkpointer——多轮/HITL 续跑（clarify 后答复）测不了；b) trace 无 session/`variant:`/`model:` 标签（那些在 gateway worker / DeerFlowClient 两条路径注入），Traces 页只能按时间找最新条目，确认 variant 要看根 span 的 system prompt 而非 tag。
2. **批量评分 runner**（进程内嵌入式 DeerFlowClient，**不经 gateway**）——**出分与比 variant 的唯一口径**，`route_correct`/`clarify_correct` 两率：
   - `benchmark/_shared/upload_datasets.py --only user_requests`（用例源=Langfuse dataset，先灌；主池 78 条，仅深圳·2013 口径）
   - `benchmark/L1_routing/run_routing_experiment.py --run-name <名> [--model qwen-plus]`
   - 逐 variant 换 `--run-name`，Langfuse `Datasets→Runs→Compare` 横向比。不适合单条调试（跑全集才有意义）。
3. **单条路由探针** `benchmark/_shared/probe_gateway.py "<query>" [--model qwen3-8b]`（外部 HTTP、**经 gateway 全栈**）——打印单条 `[tool]` 序列 + `did_route`/`did_clarify` 判定。**只在三种场景用**（其余单条验证 F5+看板已覆盖）：a) clarify 续跑/多轮用例（F5 的硬边界）；b) 怀疑问题在 gateway 链路本身（认证/worker/SSE/metadata 注入——F5 不经过这些）；c) 需要带全套 Langfuse 标签（session/variant/model tag）的正式单条复现。断点要挂 uvicorn（dev 全栈态）。凭据放根 `.env`（`DEER_FLOW_PROBE_EMAIL`/`DEER_FLOW_PROBE_PASSWORD`，已 gitignore）。

### 4.2 本阶段已定/已修

- **提示词加载根治 cwd 依赖（2026-07-11）**：CE 提示词版本库迁至 `benchmark/prompts/`（`lead_agent_v1.md` 现役 / `lead_agent_v2.md` 评测 variant，映射表见其 README.md）；`resolve_system_prompt_file()` 多基座解析（project_root→backend→仓库根），任意 cwd 都能加载；文件解析不到时 variant 标签如实降级 `default`（此前静默回退内置模板还照打文件名，尺子说谎）。切 variant=改 `config.yaml` 的 `lead_agent.system_prompt_path` 一行（热加载）。**Docker 生产态靠 compose 挂载 `../benchmark/prompts:/app/benchmark/prompts:ro`**（benchmark 不在镜像里）。`_paths.py` 的 `DEER_FLOW_PROJECT_ROOT=backend` 仍保留（`import app` + `.deer-flow` 状态目录对齐用）。
- **路由判定常量（config-grounded）**：`ROUTE_TOOL_NAMES = {cost_workflow_start/node/resume/state, bill_match, cost_calc, task}`，已删 prefix 与死名 `qa.py`/`cost.py`。依据 lead 可见工具面：`ce-rag_*`/`ce-db_*` 因 `DeferredToolFilterMiddleware` 对 lead 隐藏故不收；`verify_norm`/`verify_cost`/`cost_recall_exemplars` 非路由入口。
- **清单匹配单一双模工具（2026-07-12 合并落地）**：`bill_match(feature, code?, spec?)` —— code 缺省=选码（召回→门限选定→缺特征提醒 + few-shot 纠正示例 `exemplar_hints` 引擎内置）、code 给定=核实（真值存在性+特征 diff+召回交叉核对），原单核实工具 `verify_bill_code` 退役。引擎单源 `bill_match_engine.py`（2026-07-11 沉淀），workflow 选码节点同底座、契约不变。**spec 过 agent 面口径闸**（`CE_COST_AGENT_SPECS` 默认仅 2013，2024 服务层硬拒；ce-rag 侧同款 `CE_RAG_AGENT_STANDARDS`）。
- **Langfuse 定位定案**：判官=本地 Python 判定函数、模型=runner/gateway 调、**Langfuse 只当账本**（收 trace + `create_score`）。不上 Langfuse 原生 evaluator（确定性逻辑装不下 + SSRF 拦内网模型）；Prompt Experiment 上传路径（`upload_prompts`）已删。
- **clarify 单列红线**：`ask_clarification` 触发 HITL（`ClarificationMiddleware`→`Command(goto=END)` 中断等人），门 0.95，**不并入路由分**（红线独立计分）。
- **已删过时机制**：`CE_ROUTE_CONTEXT_URL`/RouteContextMiddleware（早在 `3691cbd4` 移除，本次清文档残留）。

### 4.3 下一步（按序）

1. **复跑路由**确认新常量修对（`route_correct` 应回升）；`[tool]` 若冒出集合外的真实名 → 补进 `ROUTE_TOOL_NAMES`。
2. **归因失败用例**（翻 trace）：分「测量错/服务没起/agent 真错」三类，别对着坏尺子调提示词。
3. （深化，可选）`task` 光看名字分不清 cost/norm → 收 `task` 的 `subagent_type`，把「路由**对不对**」也量起来（现只量「有没有路由」）。
4. **两态 base_url**：dev gateway 调本地 qwen3-8b 须 `config.yaml` base_url=`localhost:8099`（Docker 态才 `host.docker.internal:8099`）。
5. 路由稳后**铺开** toolcall/cost_task/norm_faithful——同样先对齐各自判定常量的真实工具名。
