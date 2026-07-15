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
- POC 代码放项目根下（`ce-code/` 知识层，与 `backend/` 平级），正常 commit 同步。端口约定：ce-rag :8100 检索 / ce-db :8102 结构化真值（均为 MCP 服务，供 backend agent 消费）。**原 `ce-services/` 任务层（:8101）已整体退役**——组价编排/规范问答已内嵌进 backend（`cost_workflow_*` + norm-qa/cost-agent 子智能体 + ce-rag/ce-db MCP）；其唯一遗留的选码评测引擎已迁至 `benchmark/L2_gating/select_eval/`。cost-agent 子智能体亦已退役（2026-07-12，单点能力全部引擎化为 lead 直调工具）
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

## 4. Benchmark 测试进度与续测入口（2026-07-14）

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

- **v3 提示词落盘（2026-07-12，基准评测 variant）**：`lead_agent_v3.md` = v2 查表骨架 × 现行六能力全对齐——11 行路由表每行带参数语义（bill_match 的 code 双模 / price_query 的 periods 走势 / 批量循环直调 vs 整单 workflow 分界），审计薄弱点全部闭合（resume 红线「无用户新输入严禁调」/ recommendation 连理由转述 / rates_missing 必须转述 / missing_features 转追问 / need_clarification 上抛重派）。**能力覆盖已对账**：六能力正向+信息不足+异常态全有条款，与 `ROUTE_TOOL_NAMES` 零测量缝隙。评测假设：单跳查表比 v1 两跳预分类更稳（8B）。
- **提示词加载根治 cwd 依赖（2026-07-11）**：CE 提示词版本库迁至 `benchmark/prompts/`（v1 现役 / v2 历史 / **v3 基准评测 variant**，映射表见其 README.md）；`resolve_system_prompt_file()` 多基座解析（project_root→backend→仓库根），任意 cwd 都能加载；文件解析不到时 variant 标签如实降级 `default`（此前静默回退内置模板还照打文件名，尺子说谎）。切 variant=改 `config.yaml` 的 `lead_agent.system_prompt_path` 一行（热加载）。**Docker 生产态靠 compose 挂载 `../benchmark/prompts:/app/benchmark/prompts:ro`**（benchmark 不在镜像里）。`_paths.py` 的 `DEER_FLOW_PROJECT_ROOT=backend` 仍保留（`import app` + `.deer-flow` 状态目录对齐用）。
- **路由判定常量（config-grounded）**：`ROUTE_TOOL_NAMES = {cost_workflow_start/node/resume/state, bill_match, quota_recommend, price_query, cost_calc, task}`，已删 prefix 与死名 `qa.py`/`cost.py`。依据 lead 可见工具面：`ce-rag_*`/`ce-db_*` 因 `DeferredToolFilterMiddleware` 对 lead 隐藏故不收；`verify_norm`/`verify_cost` 非路由入口（`cost_recall_exemplars` 工具注册已注销，few-shot 注入引擎内置）。
- **定额推荐引擎化（2026-07-12，能力 3）**：`quota_engine.py` 单源——取数确定性（price_compose）+ 多方案 LLM 预排（`rank_schemes` 单次结构化调用，fail-open，env `CE_QUOTA_RANK_MODEL` 可切 32B）。lead 直调 `quota_recommend` 工具；workflow 的 `select_quota` 闸复用同一预排（建议附闸载荷，选定归人）——「workflow 直接装配能力件」架构定案的首个落地。quota-recommend 子智能体退役。**多方案一律落闸**（原相似度假门限对无 score 的 schemes 恒不生效，已写实删除）。
- **智能询价引擎化（2026-07-12，能力 4）**：`price_engine.py` 单源——单期取价（`query_price`，多规格 need_review/零命中 no_source 诚实缺口）、两层启发式（`query_with_fallback`：子串 miss→近似料召回，price_review 询价候选复用）、多期走势（`price_trend`：显式期号逐期取数 + 确定性环比，按名称+规格分组不跨规格比价，C-04 差价不入 LLM）。lead 直调 `price_query` 工具（periods≥2 自动走势模式）。**region 口径闸**（`CE_COST_AGENT_REGIONS` 默认仅深圳，他省服务层硬拒——EH-03 纵深）。
- **智能计算引擎化（2026-07-12，能力 5，四引擎集齐）**：`calc_engine.py` 单源（纯函数零 I/O）——五操作（unit_price 合计/unit_rate 综合单价/line_total 合价/rollup/check）+ target-driven 链式 `compute_cost` + capability_gap 闸（无公式交人描述规则）。诚实性新增：**费率全缺 → `rates_missing` 显式标注**（综合单价退化为人材机费不再静默）。`cost_calc` 工具补显式参数（components/费率/quantity 等，payload 兜底、显式优先）。nodes 计算家族打薄为引擎薄壳。
- **cost-agent 子智能体退役 + skill 改名（2026-07-12）**：单点能力全部引擎化后其职责各归其位（单点/少量选码→`bill_match`、询价→`price_query`、成规模→workflow），批量场景由 lead 循环直调覆盖。skill `cost-agent` 改名 **`cost-workflow-guide`**（重名歧义消除），内容收窄为 workflow 操作手册（节点 payload/闸语义/resume 契约）。**子智能体反问断链修复**：norm-qa 摘除 `ask_clarification`（子智能体链无拦截中间件、问题文本会丢），缺实质信息改返回 `need_clarification` 结构化字段，lead 转问后重派——HITL 出口全系统唯 lead。
- **workflow 打薄（2026-07-12）**：`workflow.py` 只留装配（图机制/游标/暂停/事件/通用 resume 分发），**业务全部下沉 `stages.py`**（阶段契约层：每 Stage 声明 `run(state,item)` + `resume(state,item,decision)`，含 payload 装配/纠正采集/settle 人工试算落位）。要把某步从 tool 升级 agent/LLM 只改 stages 该步的 run，workflow 不动——装配与业务解耦、各自独立演进。
- **清单匹配单一双模工具（2026-07-12 合并落地）**：`bill_match(feature, code?, spec?)` —— code 缺省=选码（召回→门限选定→缺特征提醒 + few-shot 纠正示例 `exemplar_hints` 引擎内置）、code 给定=核实（真值存在性+特征 diff+召回交叉核对），原单核实工具 `verify_bill_code` 退役。引擎单源 `bill_match_engine.py`（2026-07-11 沉淀），workflow 选码节点同底座、契约不变。**spec 过 agent 面口径闸**（`CE_COST_AGENT_SPECS` 默认仅 2013，2024 服务层硬拒；ce-rag 侧同款 `CE_RAG_AGENT_STANDARDS`）。
- **Langfuse 定位定案**：判官=本地 Python 判定函数、模型=runner/gateway 调、**Langfuse 只当账本**（收 trace + `create_score`）。不上 Langfuse 原生 evaluator（确定性逻辑装不下 + SSRF 拦内网模型）；Prompt Experiment 上传路径（`upload_prompts`）已删。
- **clarify 单列红线**：`ask_clarification` 触发 HITL（`ClarificationMiddleware`→`Command(goto=END)` 中断等人），门 0.95，**不并入路由分**（红线独立计分）。
- **已删过时机制**：`CE_ROUTE_CONTEXT_URL`/RouteContextMiddleware（早在 `3691cbd4` 移除，本次清文档残留）。

### 4.3 v4 路由器版评测结果 + 待修 bug（2026-07-13/14 存档，续测从这里开始）

> **⚠️ 本节部分结论已被 2026-07-14 复跑证伪，见文末「2026-07-14 更新」——尤其「扣掉污染 norm 逼近满」不成立，A25 归因也错了。三个 bug 已修但被用户裁定回退（现代码=未修态），下节读完再判。**

**本轮结论（部分已证伪，见文末更新）：agent 路由/委派能力已达标，分数被基础设施 bug 系统性压低约 20pp。** 三个 variant 迭代（v3→v4→修 bug）后，**扣除 MCP 崩溃/token 溢出的测量污染**，真实指标：**路由率 ≈90% / clarify ≈83% / 路由对不对(norm) ≈84.6%（11/13）**。报告原始值 route 86.7% / clarify 83.3% / norm 64.7%(11/17) —— norm 的 6 条失败里 4 条是 MCP 跨 loop 崩溃/A25 token 溢出跑空（`工具=[]`，测量污染，剔除），仅 2 条 A7/CC9 是 8B 真误判。

**已做的关键修复（均已 commit/push）**：
- **提示词收敛为纯路由器**：`lead_agent_v4.md`（105→59 行）—— lead 只做「意图识别→路由」，workflow 闸机制下沉 `cost-workflow-guide` skill、复核 verdict 下沉 `cost-critic` 子agent、转述话术下沉工具 description。**瘦身让 route 65→87%、clarify 50→89%**（v3→v4 单步跃升，方向验证）。clarify 收敛为**意图/对象不明专用**（业务缺料改走 route→工具返回 missing→lead 转述 reactive），不再让 lead 主动判业务缺料。config 默认仍 v3，v4 靠 sed 切换测。
- **「路由对不对」指标 `subagent_route_correct`（§3.3-3 落地）**：runner 收 `task` 的 `subagent_type`，对金标 `metadata.agent` 落点为子智能体的用例判委派是否派对（`AGENT_TO_SUBAGENT={norm-qa:norm-qa}`，17 条）。**捕获修**：subagent_type 要从 `type=values` 状态快照的完整 messages 读（流式分片抓不全 task args，实测 `工具=['task']` 但 subagent_ok=False 的假阴性）。
- **子智能体递归上限**：`recursion_limit=max_turns`（executor.py:487）。norm-qa 10→25、cost-critic 8→20（10 步≈4 轮，agentic RAG 多轮检索+verify 跑不完会 GraphRecursionError）。**但这只是缓解，根因是下面的 MCP bug**。

**⚠️ 三个待修 bug（拖垮 benchmark，都是 backend 真 bug，非路由/提示词问题）**：
1. **MCP 会话跨 event loop 崩溃（最高优先，`[backend]`）**：`RuntimeError: Attempted to exit cancel scope in a different task`。根因 —— MCP 会话池 key=`(server_name, thread_id)`（`scope_key=thread_id`，`mcp/tools.py:134`），子 agent 与 lead **共用 thread_id 但跑在不同 loop**（子 agent 走 `_isolated_subagent_loop`，`subagents/executor.py:139`）；子 agent 调 ce-rag/ce-db MCP 时 `loop is current_loop` 为 False（`mcp/session_pool.py:70`）→ evict 分支 `await cm.__aexit__`（:90）在异于创建它的 task 里退出 anyio cancel scope → 崩 → 8B 重试风暴撞递归上限。**只有子 agent 碰 MCP 触发；lead 直调工具同 loop 不崩**。**修复方案 A（已设计待实现）**：池 key 掺 loop 身份 → `(server_name, thread_id, id(current_loop))`，每 loop 持独立会话、永不跨 loop evict；删 :70-77 跨 loop evict 分支；`close_scope`/`close_server` 的 key 过滤适配三元组。备选 B（run_coroutine_threadsafe 回原 loop 关）治标不选、C（norm 检索引擎化退役 norm-qa）丢隔离不选。要 TDD：同 loop 复用/跨 loop 隔离互不 evict/回归不抛 RuntimeError。
2. **`verify_cost` 数据结构崩（`[backend]`）**：`AttributeError: 'int' object has no attribute 'get'`（`backend/app/ce/cost/verify.py:141`）—— `(m.get("price") or {}).get("value")` 假设 price 是 dict，实际传进来是 int。cost-critic 复核链上的真代码错，与 MCP 无关。
3. **A25 上下文溢出**：cost 侧某条 37560 token 撞 qwen3-8b 32k 上限（400 BadRequest）。可能 workflow 装配了整单数据进 lead 上下文，需查是哪步没隔离。

**下一步顺序（原计划，已被下方更新覆盖）**：① 修 MCP 跨 loop（方案 A）+ verify_cost `int.get` → benchmark 分数会自然跳上来（预计 norm 逼近满、route 90%+）；② 修好后复跑确认 `GraphRecursionError`/cancel-scope 消失；③ 定 config 默认切 v4；④（可选）金标重标：那 ~12 条业务缺料 clarify 用例改判 expect_route（clarify 切片将只剩意图二义）；⑤ 铺开 toolcall/cost_task/norm_faithful。

**环境备忘**：嵌入式 runner 在宿主机跑须 `config.yaml` base_url=`localhost:8099`（Docker 态才 `host.docker.internal:8099`）；本地改 config 后 pull 用 `git stash / pull / stash pop` 保住 v4+localhost 本地差异。

---

### 2026-07-14 更新：三个 bug 已修·已验·**被回退**；§4.3 归因订正

**代码现状**：三个 bug 全部 TDD 修复过并在服务器验证，但**用户裁定硬回退**——`git reset --hard 63ec1e52` + force-push，现 `origin/main = 63ec1e52`，**三个 bug 回到未修态**（跑整单会再撞 cancel-scope / verify_cost 崩 / 溢出）。修复完整存于**本地备份**（未 push）：分支 `backup/before-subagent-revert-20260714` + 标签 `backup-20260714-1745`，均指向 `17d66a0e`；要找回 `git cherry-pick 7e0c8b67`（三 bug + 泄漏修复）/ `17d66a0e`（benchmark 第一跳）。回退的直接动因是「撤销今天关于 subagent 的更改」，但那处（`reset_subagents_config()` + conftest fixture）与三 bug 挤在同一 commit，遂整体回退。

**修复内容（备份里，供重做参考）**：① MCP 跨 loop——池 key 掺 `id(loop)` 三元组 + 全路径 loop-aware 关闭（方案 A，另修了 LRU/close_scope 跨 loop 关闭的连带同类崩溃）；② `verify_cost`——`_price_value()` 兼容标量/`{value}` dict；③ A25 溢出——workflow 工具返回过 `_lead_view` 投影（剥离整单原始 items/events）+ `full_workflow_state()` 给测试；④ 附带修了 CE 子智能体（norm-qa/cost-critic）注册泄漏进 `_subagents_config` 单例导致 `test_subagent_prompt_security` 按顺序偶挂（`reset_subagents_config` + conftest autouse，前后都重置）。本地单测全绿；全量 3775 passed（10 failed 全是 live/需模型 + 1 条陈旧 v1 断言，无关）。

**⚠️ §4.3 归因订正（复跑实证，最有价值的结论）**：
- **Bug 1（MCP 跨 loop）修复确实有效**：服务器复跑全程无 `cancel scope in a different task`，子 agent MCP 往返正常（trace 里 [4] subagent_ok=True、[23] ce-rag 业务错误能正常返回）。**这条是真崩溃，修对了。**
- **但「扣掉污染 norm 逼近满」= 证伪**：修好后 `fix-3bugs` 复跑 **norm 仍 11/17（64.7%，与修复前逐位相同）**，route 84% / clarify 89%。如果那 6 条 norm 失败真是 MCP 崩溃污染，修好后该恢复——没恢复，**说明 §4.3 把它们误判成污染了**。
- **Bug 3「A25 上下文溢出」归因两处错**：① **A25 是 norm-qa 项**（`按GB50011 框架抗震等级` 边界问答），**根本不走 cost workflow**，`_lead_view` 对它零影响；② 真实溢出源是 **lead 侧累积**（单条 query 自己的 agentic loop 打转 / 批量直调工具返回堆积到 35939 token），**不是「workflow 装配整单进 lead」**。A25/A26/A27 三条 `工具=[]` 的真相是 **8B 在「点名未收录规范」的边界 query 上不委派 norm-qa、直接自答**——**纯路由/提示词问题，与三个 infra bug 无关**。norm 的坑归下一步提示词侧治，不是基础设施。

**新暴露的两个尾巴（独立问题，都不在三 bug 里）**：
1. **lead 主图打转**（如 B10 `给"C30现浇混凝土独立基础"组价`，c6 整单）：8B 反复调同类工具不收敛 → 撞递归上限 100（`GraphRecursionError`）或先撞 32k token 墙（400）。是模型编排短板，治法=治打转/早停 + summarization 兜底 + 直调工具返回投影，非 infra bug。
2. **ce-rag 口径闸不拆逗号标准**（ce-code 层）：norm-qa 把两个标准逗号拼成 `'gb50500-2013,gb50854-2013'` 传给 ce-rag，闸精确匹配只认单个 → 拒（`不支持的规范口径`）。修：ce-rag 闸支持逗号/列表逐个校验，或 norm-qa 一次传一个。

**benchmark 方法论订正（重要，也在备份 `17d66a0e` 里）**：**L1 路由基准应只测「第一次工具决策」**——路由对错在 agent 第一个带 tool_calls 的 AI 消息就定，后续工具执行/多轮往返与路由判定无关却会累积溢出/打转/依赖服务。改法：`_drive_agent` 捕获首个带 tool_calls 的 `values` 快照即 break（break 在工具节点执行前、本 thread MCP 会话未建 → 无跨 loop 关闭风险）。副产品：**不需要 ce-rag/ce-db 起服务、无 400/递归/ConnectError 噪声**。`first-decision` 复跑 route 85% / clarify 78% / norm 71%（数字干净可比，但 8B 非确定需复跑 2~3 轮取稳定值；clarify 掉 2 条可能是「先动手再反问」被更严口径抓出、也可能噪声）。端到端整单闭环归 L3/L7，不塞进路由基准。

**真·下一步**：① 决定是否从备份重做三 bug 修复（Bug 1 确证有效，值得留）；② norm 的坑走**提示词**（让 8B 在点名未收录规范的边界 query 上照样委派 norm-qa，而非自答）；③ 若采纳「第一跳即停」基准口径，从 `17d66a0e` 挑回并复跑 2~3 轮定基线；④ 两个新尾巴（lead 打转 / ce-rag 逗号闸）按需排。

---

### 2026-07-14（续）：norm-qa 子智能体 → **skill 化**（config + skill + v3/v4 + L1 benchmark，已改未复跑）

**动因**：subagent 隔离对**单跳规范问答**收益有限，且 §4.3 那批 MCP 跨 loop 崩溃 / 递归上限坑**集中在子智能体链上**（只有子 agent 碰 MCP 才触发跨 loop evict）。把 norm-qa 从委派子智能体收敛为 lead 亲自做的 skill，绕开子智能体链这摊坑，也让 lead 意图→执行更直。**走 A 方案**：ce-rag_* 保持 deferred，lead 用前经 `tool_search` promote（不常驻 lead 工具面，避免 8B 工具面膨胀）。

**已改（工作区，未复跑验证）**：
- **新建 `skills/public/norm-qa/SKILL.md`**：移植原子智能体 prompt（口径固定深圳·2013 / agentic RAG 拆子问题 / 定稿调 `verify_norm` 回查 / 零召回诚实拒答）；新增「用前 `tool_search` promote ce-rag 检索工具」；缺实质信息改为**直接 `ask_clarification`**（lead 同进程内有用户通道，不再走 `need_clarification` 上抛——那是子智能体断链的补丁，skill 化后不需要）。
- **`config.yaml`**：删 `subagents.custom_agents.norm-qa` 整块（留退役注释），现只剩 `cost-critic`。`verify_norm` **仍注册为 lead 工具**（group norm），skill 指令引用它即可。**关键红线：没给 skill 加 `allowed-tools`**——该字段是**全局收窄不是授予**，任一启用 skill 声明它会把 lead 整个工具面锁进并集、炸掉 cost_workflow_*/bill_match 等（`allowed_tool_names_for_skills` 语义）。
- **提示词 v3（现役）+ v4 同改**：routing 表规范行 / dispatch / clarify——从「`task` 派 norm-qa 子智能体」改为「按 norm-qa skill 自做：`tool_search` promote ce-rag → agentic RAG → `verify_norm` 回查」。v0/v1/v2 历史变体未动（config 不指向，留作历史）。
- **L1 benchmark `run_routing_experiment.py`**：① `ROUTE_TOOL_NAMES` 收 `tool_search` / `verify_norm`（保留 `ce-rag_search_clause`），对 `tool_search.enabled` 开/关两态都稳（开=首动作 tool_search、关=直调 ce-rag）；② `AGENT_TO_SUBAGENT` 清空（norm-qa 不再是委派靶，`subagent_route_correct` 对本集变 nan，汇总已兜底）；③ 注释块 + 打印标签同步。**金标数据 `user_requests.jsonl` 不动**（norm 用例 `expect_route=true` 语义不变，`agent:"norm-qa"` 仅分类标签）。

**这对 §4.3「真·下一步 ②」的影响**：那条「让 8B 委派 norm-qa」的路径**已不存在**——norm 现由 lead in-context 做；norm 边界拒答坑（A25/A26/A27：点名未收录规范却自答）仍归**提示词侧**，但落点从「lead 委派条款」变为「norm-qa **skill 指令**让 lead 照样先检索再拒答」。

**未做 / 待验（复跑前必过）**：
- **8B 实测校准**：`tool_search` promote ce-rag 是**新增一跳**，弱模型可能漏 promote 直接自答 → 走 §4.1 F5→runner→trace 校准 `ROUTE_TOOL_NAMES`（若冒出 get_clause/expand_clause_refs 等其他 ce-rag 原语照实补）。dev 态验证前 config base_url 翻回 `localhost:8099`。
- **L6 `norm_faithful` 未动** ⚠️：它也走 lead 做 norm QA（DeerFlowClient），skill 化后功能上仍能跑，但 `_observe`/`score_case` 的**证据抽取可能按旧「子智能体 trace 结构」取 evidence**，lead 亲自做后 trace 形状变了，需服务器看真实 trace 确认要不要跟着调。独立一摊，本次只做 L1。

**2026-07-14 尾声：服务器 merge 又被硬回退——当前基线 = `017ff706`**：force-push `017ff706` 后，服务器侧有人把「三 bug 修复（`7e0c8b67`）+ config 切 v4 + benchmark 第一次决策重写（`752a5032`/`17d66a0e`）」merge 回 `origin/main`（merge `366c75eb`）。用户裁定**再次硬回退**：Mac + 服务器均 `git reset --hard 017ff706` + force-push，现 **`origin/main` = Mac = 服务器 = `017ff706`**。故**当前真实基线**：
- config 默认 **v3**（不是 v4——v4 切换随 merge 被丢；但 v3/v4 两份提示词都已含 norm→skill 改动）；
- **三个 infra bug 修复又回到「未修态」**（`session_pool.py` 无 `loop_id`）——但 **norm-qa skill 化本身规避了 Bug 1 对 norm 的影响**（lead 同 loop 调 ce-rag，跨 loop 崩溃只在子智能体链；现仅 `cost-critic` 子智能体碰 MCP 仍暴露）；
- **L1 runner = 老全执行版**（第一次决策重写随 `17d66a0e` 被丢）——跑时真执行工具、需 ce-rag/ce-db 起齐、仍有 overflow/递归噪声；我的 norm 信号（`tool_search`/`verify_norm`/`AGENT_TO_SUBAGENT={}`）在。
- 三 bug 修复 + 第一次决策口径完整存于备份分支 **`backup/before-subagent-revert-20260714`**（标签 `backup-20260714-1745`，指向 `17d66a0e`）：要重做 `git cherry-pick 7e0c8b67`（三 bug）/ `17d66a0e`（first-decision）。**注意 §4.3 复跑实证 Bug 1 修复真有效**——整单/cost-critic 碰 MCP 会再撞 cancel-scope，重做与否需权衡。

---

### 2026-07-15：L1 路由从 64%→98% 收口（提示词 v4→v6 + 金标校准 + 编造红线）

**当前基线 = `origin/main` = `3f7cf703` 系列**：config 默认 **v6**（`benchmark/prompts/lead_agent_v6.yaml`）；提示词已全部 **md→单块 yaml**；L1 runner = **第一次决策版**（只测首个 tool_calls 决策，工具不执行，不依赖 ce-rag/ce-db 起服务、无 overflow/递归噪声）；cost-critic **摘掉全部 MCP 工具**（只留 verify_cost）→ 全系统无子智能体碰 MCP，Bug 1 绕开。金标已 revert 回原始 clarify 口径 + A27 单条改判。

**分数轨迹（route / clarify，第一次决策版口径）**：`错标 64% → revert 85% → v5 89% → v6-1 95.65% → v6-2 97.78%`（clarify 全程 ~89%）。

**本轮基建改动**：
- **runner 切第一次决策版**（从被回退的 `366c75eb` 捞回该单文件 + 我的 norm 信号，未带三 bug 后端修复）：首个带 tool_calls 的 AI 消息即 break、工具不执行。`工具=[]` 从此**只剩「真自答」**，不再混「跑死了（overflow/递归/打转）」——归因不用翻 trace 剔噪声。每条打印 `query=...` 便于肉眼归因。
- **cost-critic 摘 MCP**（ce-db_*/ce-rag_* 全删，只留 verify_cost）：语义复核那半下线，但换来「无子智能体碰 MCP」→ Bug 1 在 CE 流程彻底绕开，不必依赖三 bug 修复。
- **提示词 md→单块 yaml**（`[backend]`）：`lead_agent_v0~v6.yaml`，顶层 `system_prompt: |` 块。加载器 `_resolve_system_prompt_template` 对 `.yaml/.yml` 取 `system_prompt` 字段、其余扩展名整段当模板；variant 标签用 `.stem` 不受扩展名影响。占位符只准 6 个白名单（多写一个 `.format` KeyError 打挂）。

**金标校准两次（方法论教训）**：
- **业务缺料重标→revert**：一度把 14 条零特征题（「帮我算算这面墙」「这个柱子套什么码」）`expect_clarify→false`（想走 route→missing→reactive）。但实测 **8B 对零特征题主动反问才对**（「这面墙」直接 route 给 bill_match 没法用），且与原始金标一致——**revert，route 64→85%**。教训：金标要贴合 8B 合理行为，别拿理论口径硬掰。
- **A27 单条改判** `expect_route true→false`：「按2024版装配式评价标准…」点名「2024版」= 版本口径超范围，该直接拒、不必白跑检索，与 B3/A18 等 2024 用例对齐（原误标 expect_route=true → route 95.65→97.78%）。

**提示词工程 v4→v6（按 query 的归因，最有复现价值）**：
- **`<norm_qa>` 主图硬闸（v5）** 治 norm 不路由：`现浇板工程量怎么算`(A20，在库计量题) v3 走 tool_search、v4 瘦身后自答——v4 只说「按 norm-qa skill 自做」没把「先检索」立成硬步骤。v5 写死「任何规范题第一步无条件 `tool_search` 取 ce-rag、拿到结果前不答不问」→ A20/CC10/CC9 回收。**教训：给 8B 写提示词要把隐含步骤全显式化，瘦身别把关键步骤砍了。**
- **真工具名替「norm-qa」措辞（v5）** 治工具幻觉：`独立基础和条形基础包含哪些`(F11)、A8/A20 曾去调**不存在的 `norm-qa` 工具**——提示词写「按 norm-qa skill」被 8B 当工具名。改用真名 `tool_search`/`ce-rag_search_clause`/`verify_norm`。**教训：提示词工具名必须 = config 注册名（= `ROUTE_TOOL_NAMES`），别造词。**
- **拆分「口径超范围」vs「条文题」+ 拒答禁编（v6，最关键的安全修复）** 治**编造红线违规**：`按GB50011 抗震等级`(A25) trace 实证——8B 把「点名 GB50011」误当口径超范围（套用 v5「2024/他省→不检索直接拒」红线）→ 跳检索 → **拒答时编造** `DBJ15-9-2019`/`GB18306-2015`/甲乙丙丁类划分（全是造的！L1 的 route_ok 抓不到）。v6：① 写死「**口径超范围只指 版本(2024)/地区(他省)/专业(安装)**，点名国标问条文=条文题走 `<norm_qa>`」；② 最高红线「**拒答/超范围时禁编**」——只说超范围+建议咨询，绝不给具体条文号/标准号/数值/等级。**实测 A25 从「自拒+编造」变「`工具=['tool_search']` 先检索」。教训：① 红线措辞过宽会被弱模型泛化吃掉别的规则，边界要精确定义；② L1 两率抓不到编造，边界/拒答的真实安全风险在 L6 忠实性。**
- **clarify 补「清晰题不反问」（v6）** 治过度反问蔓延：`水泥这阵子涨了吗`(CC8)、`这个混凝土现在贵不贵`(F6，材料已点名)、`现浇C30独立基础和预制杯形基础哪个省`(P1，构件带规格) 曾被过度反问——v4 clarify 例子「帮我算算」太宽把清晰题也带反问。v6 显式豁免「材料已点名的询价 / 构件+规格都给的比选 直接 route，不反问」。**教训：8B 靠例子做模式匹配，例子选不准会反噬。**

**clarify 的结构性天花板**：卡 ~89%（16/18），门 0.95。**n=18 时 17/18=94.4% 仍 <0.95，须 18/18 才过**。2 个缺口 = F5（业务缺料没反问，噪声）+ B37（`这块现浇板套什么定额` 调了 bill_match 而非 clarify——但这是「route→missing→reactive」合法路径，**假缺口**）。业务缺料题 route/clarify **两种都对**，逼满 18/18 = 跟合法灵活性死磕。故 clarify ~89% 可能是现实上限，除非松门或接受「业务缺料 route 也算对」。

**未做 / 下一步**：
- **A25 编造终审未做** ⚠️：第一次决策版在 `tool_search` 就 break，只证明 A25「走上检索路」，**没跑到 ce-rag 零召回后的终答**——要 F5 全执行 或 L6 norm_faithful 看拒答正文是否真不编 DBJ 假号。
- **v6 稳定性**：v6-2 才 2 轮，需 v6-3 复稳（8B 非确定，单轮 ±噪声）。剩 1 条 route 失败大概率是 F5 噪声。
- **cost-critic 语义复核下线的代价**：选错码/语义错配它不再抓（只剩 verify_cost 算术/编码/串库/缺价），若要补回需换非子智能体方式（避免 Bug 1）。
- **L6 `norm_faithful` 仍未动**：edge/边界题的真实风险（编造）在这层量，L1 收口后应转 L6。
