# CE lead-agent 提示词版本库（variant 注册表）

lead_agent 实际使用的系统提示词由 `config.yaml → lead_agent.system_prompt_path` 决定；
本目录集中管理所有版本，**「实际用的 ↔ 版本文件 ↔ trace 标签」三者由同一条链路保证一致**：

```
config.yaml 指向本目录某文件
  → prompt.py 经 resolve_system_prompt_file() 加载（cwd 无关：project_root → backend → 仓库根 依次探测）
  → tracing/metadata.py 用同一解析函数打 variant 标签 = 文件名 stem
  → 文件解析不到 = 提示词回退内置模板，variant 如实打 default（不再冒充文件名）
```

## 版本映射表

| 文件 | variant 标签 | 设计要点 | 状态 |
|---|---|---|---|
| `lead_agent_v0.md` | `lead_agent_v0` | **harness 内置默认模板逐字复刻**（= `prompt.py:SYSTEM_PROMPT_TEMPLATE`，无任何 CE 定制/路由/红线）。保留全部运行时占位符（`{skills_section}`/`{subagent_section}`/… 运行时注入，与 v1/v3 同条件），故与下方 `default` 回退行为**完全等价**，仅多打一个显式 `lead_agent_v0` 标签——用作对照基线，量「CE 定制相对原版净收益」 | 对照基线 |
| `lead_agent_v1.md` | `lead_agent_v1` | capability 预分类（norm/cost/both）两跳路由；单点四能力全部直调（`bill_match`/`quota_recommend`/`price_query`/`cost_calc`），批量循环直调；子智能体仅 norm-qa（隔离派）+ cost-critic（复核）；need_clarification 上抛转问 | 现役 |
| `lead_agent_v2.md` | `lead_agent_v2` | 查表式单跳路由（诉求特征→动作）；单点匹配/定额推荐/计算直调；能力清单为旧五能力 | 历史 variant（被 v3 取代） |
| `lead_agent_v3.md` | `lead_agent_v3` | **v2 查表骨架 × 现行六能力全对齐**（2026-07-12）：11 行路由表每行带参数语义（bill_match 的 code 双模 / price_query 的 periods 走势 / 批量循环直调分界）；resume 红线（无用户新输入禁调）+ recommendation/rates_missing/missing_features 转述条款 + need_clarification 上抛——审计发现的全部薄弱点闭合。**评测假设：单跳查表比 v1 两跳预分类路由更稳（8B）** | 基准评测 variant |
| `lead_agent_v4.md` | `lead_agent_v4` | **v3 瘦身版（2026-07-13）**：lead 收敛为纯「意图识别→路由」。路由表当骨架（norm/复核的 task 分派并入表内），红线压到 4 条；workflow 闸机制下沉 `cost-workflow-guide` skill、复核 verdict 下沉 `cost-critic` 子agent、转述话术下沉工具 description——砍掉能力清单与业务细节，105→57 行。**评测假设：更瘦的路由器让 8B 少受业务噪声干扰、路由/委派更准**（对照 v3 验证 subagent_route_correct 是否回升） | 精简评测 variant |
| （无文件 / 解析失败） | `default` | harness 内置英文通用模板（`prompt.py:SYSTEM_PROMPT_TEMPLATE`，已恢复上游原版），无 CE 路由 | 回退兜底 |

> v1/v2 均含 `<safety_redline>`（串库红线 + 禁编造，2026-07-11 自旧 CE 内置模板移植——该红线是 agent 自补 `010504001`/`E.4.1` 事故换来的，任何新 variant 都应保留）。

## 怎么切版本（对比实验流程）

1. 改 `config.yaml` 一行：`lead_agent.system_prompt_path: benchmark/prompts/lead_agent_v2.md`
   —— **热加载**，下一条消息即生效，不用重启 gateway / 重建容器。
2. 跑评测：`benchmark/L1_routing/run_routing_experiment.py --run-name <名>`（逐 variant 换 run-name）。
3. Langfuse `Datasets → Runs → Compare` 横向比 `route_correct`/`clarify_correct`；
   trace 均带 `variant:<文件名stem>` 标签，可直接过滤分组。

## 部署边界（Docker 生产态）

镜像只 `COPY backend`，本目录**不在镜像里**——生产 compose 已将 `../benchmark/prompts`
只读挂载到容器 `/app/benchmark/prompts`（与 `../skills` 同模式，见 `docker/docker-compose.yaml`
gateway.volumes）。新增/改版本文件无需重建镜像，重挂即可见；**删掉这行挂载 = 生产静默回退
内置模板**（variant 会如实打 default，Langfuse 里能看出来）。

## 加新版本的约定

- 文件名 `lead_agent_<语义名>.md`（stem 即 variant 标签，起名要能在 Langfuse 里一眼认出）；
- 模板里只能用 `apply_prompt_template` format kwargs 的子集占位符
  （`{agent_name}` `{soul}` `{skills_section}` `{deferred_tools_section}` `{subagent_section}` `{acp_section}` 等），
  多用会 KeyError；
- 引用的工具名必须是 config.yaml 真实注册名（评测判定 `ROUTE_TOOL_NAMES` 以此为准）；
- 新版本在上表加一行，写清与上一版的差异假设（评测在验什么）。

## 历史坑（为什么有这套机制）

旧布局 `backend/prompts/ce/lead_agent.md` + 相对路径按 `Path.cwd()` 解析：cwd 不是 backend/ 时
**静默回退内置模板、variant 标签却照打文件名**——调试才发现实际跑的是内置提示词，评测失真。
2026-07-11 迁至本目录并改多基座解析 + 打标如实降级，根治 cwd 依赖。
