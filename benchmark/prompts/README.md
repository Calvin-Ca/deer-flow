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
| `lead_agent_v0.yaml` | `lead_agent_v0` | **harness 内置默认模板逐字复刻**（= `prompt.py:SYSTEM_PROMPT_TEMPLATE`，无任何 CE 定制/路由/红线）。保留全部运行时占位符（`{skills_section}`/`{subagent_section}`/… 运行时注入，与 v1/v3 同条件），故与下方 `default` 回退行为**完全等价**，仅多打一个显式 `lead_agent_v0` 标签——用作对照基线，量「CE 定制相对原版净收益」 | 对照基线 |
| `lead_agent_v1.yaml` | `lead_agent_v1` | capability 预分类（norm/cost/both）两跳路由；单点四能力全部直调（`bill_match`/`quota_recommend`/`price_query`/`cost_calc`），批量循环直调；子智能体仅 norm-qa（隔离派）+ cost-critic（复核）；need_clarification 上抛转问 | 现役 |
| `lead_agent_v2.yaml` | `lead_agent_v2` | 查表式单跳路由（诉求特征→动作）；单点匹配/定额推荐/计算直调；能力清单为旧五能力 | 历史 variant（被 v3 取代） |
| `lead_agent_v3.yaml` | `lead_agent_v3` | **v2 查表骨架 × 现行六能力全对齐**（2026-07-12）：11 行路由表每行带参数语义（bill_match 的 code 双模 / price_query 的 periods 走势 / 批量循环直调分界）；resume 红线（无用户新输入禁调）+ recommendation/rates_missing/missing_features 转述条款 + need_clarification 上抛——审计发现的全部薄弱点闭合。**评测假设：单跳查表比 v1 两跳预分类路由更稳（8B）** | 基准评测 variant |
| `lead_agent_v4.yaml` | `lead_agent_v4` | **v3 瘦身版（2026-07-13）**：lead 收敛为纯「意图识别→路由」。路由表当骨架（norm/复核的 task 分派并入表内），红线压到 4 条；workflow 闸机制下沉 `cost-workflow-guide` skill、复核 verdict 下沉 `cost-critic` 子agent、转述话术下沉工具 description——砍掉能力清单与业务细节，105→57 行。**评测假设：更瘦的路由器让 8B 少受业务噪声干扰、路由/委派更准**（对照 v3 验证 subagent_route_correct 是否回升） | 精简评测 variant |
| `lead_agent_v5.yaml` | `lead_agent_v5` | **v4 + norm 路由硬闸（2026-07-15）**：针对 v4-orig-1 失败（7 条路由失败里 6 条是 norm）——新增 `<norm_qa>` 硬闸：任何规范/计量/条文题**第一步无条件 `tool_search` 取 ce-rag 检索**、拿到结果前不答不问、**边界题也先检索确认零召回再拒**（禁止凭记忆判「没收录」跳检索）；用真工具名（tool_search/ce-rag_search_clause/verify_norm）替 v4 会致工具幻觉的「norm-qa」措辞；红线 + 路由表给 2024/他省补「**不派 task**」（治 B3 派 task 组价 2024）；clarify 补「规范/域外/2024 三类不反问」。**评测假设：norm 那 6 条路由失败可回收，route 从 84.78% 再上台阶** | 精简评测 variant |
| `lead_agent_v6.yaml` | `lead_agent_v6` | **v5 + 掐死拒答编造（2026-07-15）**：A25 trace 暴露 8B 把「点名 GB50011」误当口径超范围、跳检索且**编造 DBJ15-9-2019 等假标准号**顺手作答（编造红线违规，L1 route 指标看不见）。v6：① red_lines 加最高红线「**拒答/超范围时禁编**」——只说超范围+建议咨询，绝不给具体条文号/标准号/数值/等级；② 明确「**口径超范围只指 版本(2024)/地区(他省)/专业(安装)**」，点名国标问条文是条文题走 `<norm_qa>`，别套红线直接拒（消解 red_lines 吃掉 norm_qa 的冲突）；③ clarify 补「材料已点名询价 / 构件+规格比选 不反问」（治 v5-1 的 F6/P1 过度反问）。**评测假设：掐死编造是安全红线优先；F6/P1 回收；边界题真实风险归 L6 忠实性** | 被 v7 取代 |
| `lead_agent_v7.yaml` | `lead_agent_v7` | **v6 + 工具失败路径禁编（2026-07-16）**：A25 全执行 trace 暴露——`tool_search` 报错后 8B 没认怂，**改调 `verify_norm` 并编造答案+假证据硬凑**（v6 的禁编红线只覆盖「拒答/超范围」，没覆盖「工具失败」路径）。v7 加最高红线：**工具报错/返回空/检索不到时，如实说不可用、不改调别的工具凑数、不编造参数或答案**，尤其禁止把编造的条文/证据塞给 `verify_norm` 假装通过。其余继承 v6。**评测假设：堵住工具失败时的编造硬凑；与 verify_norm 正则修复([backend])配套** | 现役（config 默认） |
| （无文件 / 解析失败） | `default` | harness 内置英文通用模板（`prompt.py:SYSTEM_PROMPT_TEMPLATE`，已恢复上游原版），无 CE 路由 | 回退兜底 |

> v1/v2 均含 `<safety_redline>`（串库红线 + 禁编造，2026-07-11 自旧 CE 内置模板移植——该红线是 agent 自补 `010504001`/`E.4.1` 事故换来的，任何新 variant 都应保留）。

## 怎么切版本（对比实验流程）

1. 改 `config.yaml` 一行：`lead_agent.system_prompt_path: benchmark/prompts/lead_agent_v2.yaml`
   —— **热加载**，下一条消息即生效，不用重启 gateway / 重建容器。
2. 跑评测：`benchmark/L1_routing/run_routing_experiment.py --run-name <名>`（逐 variant 换 run-name）。
3. Langfuse `Datasets → Runs → Compare` 横向比 `route_correct`/`clarify_correct`；
   trace 均带 `variant:<文件名stem>` 标签，可直接过滤分组。

## 部署边界（Docker 生产态）

镜像只 `COPY backend`，本目录**不在镜像里**——生产 compose 已将 `../benchmark/prompts`
只读挂载到容器 `/app/benchmark/prompts`（与 `../skills` 同模式，见 `docker/docker-compose.yaml`
gateway.volumes）。新增/改版本文件无需重建镜像，重挂即可见；**删掉这行挂载 = 生产静默回退
内置模板**（variant 会如实打 default，Langfuse 里能看出来）。

## 文件格式（2026-07-15 起：单块 yaml）

提示词文件是 **`.yaml`，顶层一个 `system_prompt: |` 块标量**，块内整段即 system prompt 模板：

```yaml
system_prompt: |
  你是{agent_name}，深圳市房建组价助手...
  <routing priority="高">
  ...
```

加载器（`prompt.py:_resolve_system_prompt_template`）对 `.yaml/.yml` 走 `yaml.safe_load` 取 `system_prompt` 字段，
其余扩展名（`.md/.txt`）仍把整段文件当模板。**编辑要点**：块内每行须保持 2 空格缩进（yaml block scalar 规则），
缩进一乱会解析失败并回退内置模板（有 WARNING 日志）；占位符 `{...}` 在块标量里是字面量、不受 yaml 影响。
variant 标签取 `Path(path).stem`（去扩展名），故 `.md→.yaml` 后标签不变（仍 `lead_agent_v4` 等）。

## 加新版本的约定

- 文件名 `lead_agent_<语义名>.yaml`（stem 即 variant 标签，起名要能在 Langfuse 里一眼认出）；
- 模板里只能用 `apply_prompt_template` format kwargs 的子集占位符
  （`{agent_name}` `{soul}` `{skills_section}` `{deferred_tools_section}` `{subagent_section}` `{acp_section}` 等），
  多用会 KeyError；
- 引用的工具名必须是 config.yaml 真实注册名（评测判定 `ROUTE_TOOL_NAMES` 以此为准）；
- 新版本在上表加一行，写清与上一版的差异假设（评测在验什么）。

## 历史坑（为什么有这套机制）

旧布局 `backend/prompts/ce/lead_agent.md` + 相对路径按 `Path.cwd()` 解析：cwd 不是 backend/ 时
**静默回退内置模板、variant 标签却照打文件名**——调试才发现实际跑的是内置提示词，评测失真。
2026-07-11 迁至本目录并改多基座解析 + 打标如实降级，根治 cwd 依赖。
