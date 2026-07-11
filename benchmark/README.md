# benchmark 目录地图

> 按**测试阶段（层）**组织：每层一个 `L*_` 目录，数据（`data/` 或同名 jsonl）、runner、判分器、judge 细则都住在自己层里；跨层共用的基建在 `_shared/`。**测什么/指标口径**见 [`AGENT_BENCHMARK.md`](AGENT_BENCHMARK.md)（七层规范），**怎么跑 Langfuse 全链路**见 [`LANGFUSE.md`](LANGFUSE.md)。命令一律在服务器上 `uv run --project backend python benchmark/...`（单行）。

## 目录 ↔ 层

| 目录 | 层 | 里面有什么 | runner 状态 |
|---|---|---|---|
| `L1_routing/` | L1 路由 | `data/` 2 集（路由主池 111 + 清单匹配专项 100，均带 capability×difficulty 标签，字段说明见 `data/README.md`）+ 生成脚本；`run_routing_experiment.py` | ✅ 可跑（当前主战场） |
| `L2_gating/` | L2 置信度门控 | `select_eval/` 选码引擎（召回/选码/**置信校准**/阈值），B1 调优主战场 | ✅ 引擎可跑（`tools/eval_select.py`） |
| `L3_retrieval/` | L3 检索 | `data/` 清单匹配金标（2013×91 + 2024×10 + 未覆盖码清单）+ GB50016 条文召回；`run_retrieval_experiment.py`（复用 L2 引擎） | ✅ 清单匹配可跑；条文召回待 qa 支持 gb50016 |
| `L4_redline/` | L4 答案/红线 | `adversarial/` 直接对抗 10 条 + **间接注入 5 条（盲区③）** | ⬜ runner 待建（注入需工具桩） |
| `L5_composite/` | L5 复合拆解 | 占位 README（专用金标待建，种子线索在内） | ⬜ |
| `L6_agent/` | L6 任务级 | `cost_task/`（τ-bench 式 10 条 + runner + 判分器 + 选码 judge）、`toolcall/`（BFCL 式 16 条 + runner）、`norm_faithful/`（RAGAS 式 8 条 + runner + 判分器 + 忠实度 judge）、`trajectory/`（多轮 8 条，**HITL 续跑①+效率预算④**）、`fault_injection/`（**故障注入 6 条②**） | cost_task/toolcall/norm_faithful ✅；trajectory/fault_injection ⬜ 待建 |
| `L7_nfr/` | L7 NFR | 占位 README（延迟/隔离/可观测，横切） | ⬜ |
| `component_eval/` | 不占层号 | 零件级：`listing_eval/` 列清单抽取 7 条、`critic_eval/` 核对 Critic 3 条 | ⬜ 原驱动随 ce-services 退役 |
| `_shared/` | 基建 | `_lf.py`/`_paths.py`（Langfuse/路径引导）、`upload_datasets.py`（跨层灌库）、`probe_gateway.py`（单条探针，经 gateway 全栈）、`smoke_test.py`、`dump_run_scores.py` | ✅ |
| `prompts/` | 非评测 | CE lead-agent 提示词版本库（v1 现役/v2 评测 variant）——**位置不能动**：config.yaml 按此路径引用、生产 compose 只读挂载进容器 | — |

## 测试梯队（跑分顺序，依赖链决定）

1. **第一梯队**（无依赖、机器可判，先跑绿）：L1 路由（进行中）｜L3 检索（可与 L1 并行）｜L4 红线（数据就绪，应最早进 CI、此后全程带跑）
2. **第二梯队**（依赖一梯队产物）：L2 门控（等 L3 稳定后冻结检索再调 τ/w）｜L6-B 工具调用
3. **第三梯队**（零件绿了测整机）：L6-C 规范问答忠实度｜L5 复合拆解｜**盲区专项：trajectory（HITL 续跑）+ fault_injection（故障注入）——插在 L6-A 之前，它们是 L6-A 失败模式的归因前置**
4. **第四梯队**（整机+最贵）：L6-A 端到端组价 pass^5｜L7 NFR

## 常用命令速查

```
# 灌金标（全部 / 单集）
uv run --project backend python benchmark/_shared/upload_datasets.py [--only user_requests|bill_match_routing|clist|toolcall|cost_task|norm_faithful|clause]
# L1 路由批量评分
uv run --project backend python benchmark/L1_routing/run_routing_experiment.py --run-name <variant> [--model qwen-plus] [--dataset bill-match-routing]   # 缺省跑主池 user-requests-routing
# 单条路由探针（经 gateway 全栈）
uv run --project backend python benchmark/_shared/probe_gateway.py "<query>" [--model qwen3-8b]
# L3 清单匹配（复用 L2 选码引擎）
uv run --project backend python benchmark/L3_retrieval/run_retrieval_experiment.py --spec 2024 --run-name clist_2024_v1
# L6 三子集
uv run --project backend python benchmark/L6_agent/toolcall/run_toolcall_experiment.py --run-name toolcall_v1
uv run --project backend python benchmark/L6_agent/cost_task/run_cost_task_experiment.py --run-name cost_v1 --split test
uv run --project backend python benchmark/L6_agent/norm_faithful/run_norm_faithful_experiment.py --mode traditional --run-name norm_base
```

> Langfuse dataset 名**不随目录改**（`user-requests-routing`/`bill-match-routing`/`clist-match-eval`/`agent-toolcall-eval` 等），已跑的 runs 与横向对比不受目录重组影响（2026-07-11 重组，原 `routing_eval`/`retrieval_eval`/`agent_eval`/`runner`/`scoring`/`judges`/`select_eval` 并入上表结构）。例外：`agent-routing-eval`（原 34 条冻结金标）2026-07-11 按六能力需求审并进路由主池后**停用**，其历史 runs 保留可查但不再新增。

## 嵌入式评测的隔离清单（踩坑记录）

评测有效的前提是用例互相独立。嵌入式 runner（DeerFlowClient）有**三层隔离要分别管住**，只管对话那层不够：

| 层 | 隔离机制 | 状态 |
|---|---|---|
| ① thread（对话历史） | runner 逐条独立 thread_id，checkpointer 按 thread 隔离。**坑**：checkpointer 持久化跨进程存活，`--run-name` 重名曾致 thread_id 撞旧线程、静默**续跑上轮对话**（实锤：首工具 `cost_workflow_resume`、开局 60k 撞 32k 上限）——已在 thread_id 掺进程级随机后缀根治（2026-07-12） | ✅ runner 已内建 |
| ② user（跨会话记忆） | **无鉴权嵌入式全落 `default` 用户**——`MemoryMiddleware` 按用户存记忆，跨 thread 照样注入 | ⚠️ 靠 `config.yaml` 的 `memory.enabled: false` 关死（2026-07-11 定案） |
| ③ run 顺序（跨 run 残留） | 记忆落盘跨进程存活：先跑的 variant 会把"经验"留给后跑的 variant，横向对比失真 | 同上 + 清存量 |

**② 的实锤案例（2026-07-11，路由评测）**：Langfuse trace 里发现每条用例的 system prompt 被注入了几千 token 的 `<memory>` 块——前面所有 run 的用例沉淀。危害三重：跨 run 污染 variant 对比；撑爆 qwen3-8b 的 32k 上下文（帮凶）；**行为级带坏**——某次服务没起时的失败被学成 fact「cost-agent tool not available (avoid)」注入后续每条用例，直接劝退路由（route_ok=False 冤案）；历史候选码进记忆后模型可凭记忆答码（串库红线场景），答对了分数反而虚高。

**记忆污染的时序特征**（为什么难察觉）：注入发生在消息处理开头（读旧快照），提取在整轮结束后 debounce 30s + LLM 提炼才落盘——污染**隔轮生效**，不毒害紧邻的下一条，而是悄悄毒害之后的所有 run。

**处置**：`memory.enabled: false` 单开关同时关提取与注入（`memory_middleware.py` / `prompt.py` 两处 gate）；重开记忆前**必须**先清已污染存量：`rm -f backend/.deer-flow/users/default/memory.json`（`default` 目录只有嵌入式评测/调试的沉淀，无真实用户数据）。若日后产品要开记忆，评测 runner 须另想隔离（per-run user_id 只隔 run 间、隔不了 run 内，关掉仍是评测期唯一可靠解）。
