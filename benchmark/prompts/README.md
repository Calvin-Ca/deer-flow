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
| `lead_agent_v1.md` | `lead_agent_v1` | capability 预分类（norm/cost/both）两跳路由；单点任务派 cost-agent 子智能体；详尽 subagent_dispatch 并行调度指南 | 线上现役基线 |
| `lead_agent_v2.md` | `lead_agent_v2` | 查表式单跳路由（诉求特征→动作）；单点核实/计算直调 `verify_bill_code`/`cost_calc`；五能力对齐 CLAUDE.md §1；clarify/discipline 红线单列 | 评测 variant |
| （无文件 / 解析失败） | `default` | harness 内置英文通用模板（`prompt.py:SYSTEM_PROMPT_TEMPLATE`，已恢复上游原版），无 CE 路由 | 回退兜底 |

> v1/v2 均含 `<safety_redline>`（串库红线 + 禁编造，2026-07-11 自旧 CE 内置模板移植——该红线是 agent 自补 `010504001`/`E.4.1` 事故换来的，任何新 variant 都应保留）。

## 怎么切版本（对比实验流程）

1. 改 `config.yaml` 一行：`lead_agent.system_prompt_path: benchmark/prompts/lead_agent_v2.md`
   —— **热加载**，下一条消息即生效，不用重启 gateway / 重建容器。
2. 跑评测：`benchmark/runner/run_routing_experiment.py --run-name <名>`（逐 variant 换 run-name）。
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
