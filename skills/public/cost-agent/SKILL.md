---
name: cost-agent
description: 算量计价 CostAgent 技能。接收构件/做法的自然语言描述做组价。两种模式：① 一次性「选码+组价取数」（compose，快速查清单码/取数）；② 可中断 HITL 完整组价（start：agent 起会话并吐 cost-hitl marker，前端据此内嵌交互控件，用户在控件里逐闸确认编码/定额/价格/费率→出总造价，agent 不逐闸驱动）。覆盖深圳房建组价（清单计量国标 GB 50854 2013/2024 双版隔离）。适用于"C30现浇矩形柱组价""走完整组价流程算到总造价""某砌体墙套什么清单码"。强制：缺版本不反问默认深圳·2013（首答带口径声明）、特征不足才反问、选码只在候选内不造码、低置信转人工、缺价不杜撰、他省口径体面告知不取数、费率/税率等政策数由用户给不替填默认、HITL 闸交互交给内嵌控件不替用户决策。
---

# 算量计价 CostAgent（cost-agent）

## ⛔ 红线（最优先，先于一切回答）

1. **缺版本不反问，默认深圳·2013**（PRD §4.0/C-05）：用户没说按哪版国标组价时，**不要就版本 `ask_clarification`**，直接按默认口径深圳·2013 执行（spec 缺省即可，服务端会归一并在 `meta.caliber` 标 `spec_source=default`）。**会话内首次回答的开头带一行口径声明**：「口径：深圳·2013（未指定版本时的默认，可说明"按 2024 版"切换）」，之后同会话不重复刷屏。用户**显式**说了版本（2013/2024）就按说的传，声明行照实写。2013 组价数据未就绪时服务端如实返回「未就绪，仅选码」——原样转达，不静默换 2024。
2. **特征不足才反问**：构件描述不足以选码（如只说"柱"未说混凝土等级/现浇预制）时，先 `ask_clarification` 补全关键特征——反问只问特征，**不问版本**。
3. **他省口径体面告知（EH-03）**：用户显式要按北京/上海等他省口径组价/问价时，**不取数、不反问**，如实告知「本系统仅覆盖深圳·2013 口径」并建议当地造价站渠道；规范条文类问题可转规范问答（支持联网兜底）。
4. **选码不造码、缺价不杜撰**：只接受脚本返回候选内的编码；`need_review` / 低置信时如实告知"需人工复核"，不当定稿；`no_source` 如实透传，不补编价格。
5. **费率/税率等政策数由用户给，不替填默认**：管理费率/利润率/取费基数/措施费/规费/税率等是工程政策参数，HITL 闸会停下来问用户——**必须把用户给的值原样填进 decision，绝不自己编一个默认值**。
6. **流程编排在服务端的图里，agent 不当编排器**：你（agent）只做「把闸呈现给用户 + 收集决策 + 调 resume 续跑」，**不要自己决定跳过某个闸、不要替用户做闸内决策**。是否停闸、下一步走哪由服务端的图决定。

---

## 两种模式怎么选

| 模式 | 用 | 何时 |
|---|---|---|
| **一次性 compose** | MCP 工具 `ce-task_cost_compose`（bash `cost.py compose` 兜底） | 用户只想「知道某构件的清单码 / 工料机取数」，不需要走完整算价、不需要人逐步确认 |
| **HITL 流程 start/resume** | `cost.py start` → 前端内嵌卡片 | 用户要「走完整组价、算到总造价、要人确认编码/录入费率」——可中断、可审计 |

服务地址默认 `http://localhost:8101`（环境变量 `COST_AGENT_URL` 覆盖）。脚本是纯 stdlib 薄客户端，沙箱内零依赖。

---

## 模式一：一次性组价（compose）

**首选 MCP 工具 `ce-task_cost_compose`**（对话主路径）：直接作为 function-calling 工具调用，参数 `description`（必填）/ `spec`（可缺省，缺省=默认深圳·2013，红线 1）/ `region`（默认深圳）/ `top_k`（默认 10）。工具的**选中码、置信度、候选、取数状态会结构化渲染进对话中间过程**（用户看得见选码依据），优于 bash 把结论埋进 stdout。注册见 `extensions_config.json` 的 `ce-task`（`http://localhost:8101/mcp`），任务服务 :8101 起着即可用。

兜底（curl/无 MCP 环境）：bash 调 `cost.py compose`：

```bash
python3 /mnt/skills/public/cost-agent/cost.py compose --description "C30现浇混凝土矩形柱" --region 深圳
```

（`--spec` 缺省默认 2013；用户显式要 2024 时加 `--spec 2024`。）

返回 `{selection{code,confidence,need_review,...}, code, price{工料机+信息价}, price_status}`。
- `need_review=true` / `code=null` → 转人工复核，不当定稿。
- `price` 内 `no_source` 资源 = 缺信息价缺口，如实转达不补编。
- compose **不算钱**（不出综合单价/总造价）；要算到总造价走 HITL 模式。

---

## 模式二：可中断 HITL 完整组价（start）—— 内嵌交互控件，你只「点火」

**重要：你（agent）不逐闸驱动、不替用户做决策。** 你只做两件事：① 确认版本后 `cost.py start` 起会话；
② 把 start 输出的 `cost-hitl` 代码块**原样贴进回复**。之后前端会据此**内嵌渲染交互式组价控件**，
用户在控件里逐闸点选/录入（编码确认、定额确认、缺价录入、费率、参数、总造价复核），控件直接驱动会话——
**全程不再经过你**。这就把「弱模型不当编排器」的红线交还给了结构化控件。

### 步骤 0：先确认版本（红线 1）+ 描述够（红线 2）

### 步骤 1：起会话

**首选 MCP 工具 `ce-task_start_cost_session`**（对话主路径）：直接作为 function-calling 工具调用，参数
`feature`（必填）/ `spec`（可缺省，缺省=默认深圳·2013，红线 1）/ `region`（默认深圳）。返回
`{task_id, marker, first_gate, status}`——**把返回的 `marker`（```cost-hitl 代码块）一字不改贴进回复即可**（同下步骤 2）。
注册见 `extensions_config.json` 的 `ce-task`，任务服务 :8101 起着即可用。

兜底（curl/无 MCP 环境）：bash 调 `cost.py start`：

```bash
python3 /mnt/skills/public/cost-agent/cost.py start \
  --description "C30现浇矩形柱" --spec 2024 --region 深圳
```

stdout 会输出一个 marker 代码块，形如：

````
```cost-hitl
{"task_id": "abc123..."}
```
````

### 步骤 2：把 marker 原样贴进回复，然后停

- **把那个 ```cost-hitl 代码块一字不改地放进你的回复**（别翻译、别改成 JSON 描述、别替换成表格）。前端识别它后会内嵌出组价控件。
- 配一句话引导即可，例如：「已为你起好 C30 现浇矩形柱（2024）的组价会话，请在下面的控件里逐步确认编码、定额、价格与费率。」
- **然后停下**。不要再调 `resume`、不要用 `ask_clarification` 逐闸问、不要替用户在控件里做选择——这些都由内嵌控件 + 用户完成。

> 为什么这样：组价 13 步里每个数字错一个就是真金白银，弱模型逐闸转译用户意图易出错。把闸做成结构化控件
> 由用户直接操作（依据卡、备选按钮、字段录入都从结构化 payload 渲染），既准确又可审计——你只负责把会话点起来。

### 备用：无内嵌控件时的纯命令行兜底（一般用不到）

若环境没有前端控件（如纯 API/curl 调试），可用 `resume` 手动逐闸推进（decision 为 JSON）：
```bash
python3 /mnt/skills/public/cost-agent/cost.py resume --task <task_id> --decision '<decision JSON>'
```
- confirm 闸：`{"action":"approve"}` / `{"action":"select_alternative","value":"<code>"}` / `{"action":"manual_override","value":"<code/子目号>"}`
- input 闸（缺价/费率/参数）：字段值 dict，如 `{"value":5.5}` / `{"management_fee_rate":10,"profit_rate":5,"fee_base":"labor_machine"}` / `{"measure_fee":1000,"fee_levy":500,"tax_rate":9}`
- review 闸：`{"action":"approve"}`
- 读当前态：`cost.py state --task <task_id>`
> ⛔ 即便走兜底，费率/税率仍**必须用用户给的值，不替填默认**（红线 4）。

---

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| **任务服务** | localhost:8101 | 本 skill 调用（compose + session 三端点） |
| **知识服务** | localhost:8100 | bill_match / price_compose 取数原语（被任务服务调用） |
| Milvus | localhost:19530 | 清单向量库 |
| vLLM embedding | localhost:8097 | 文本 embedding |
| vLLM Qwen3-8B | localhost:8099 | LLM 选码 |

> 启动（服务器，常驻，先知识后任务）：
> `cd ce-code && uv run python -m service.knowledge_api`（:8100）
> `cd ce-services && uv run python main.py`（:8101）

## 常见错误排查

> 都是**服务端配置问题**，agent 在沙箱内无法补救（不要建 venv / 装包 / 拷脚本）。把错误原文转达用户。

| 错误 | 原因 | 处理（服务器上） |
|---|---|---|
| `无法连接服务` | 8101 未启动 | `cd ce-services && uv run python main.py` |
| `未知国标版本` | `--spec` 非 2013/2024 | 确认按哪版国标组价 |
| 服务返回 400（spec） | 知识服务 spec 未配 | 检查 ce-code `config.SPEC_REGISTRY` |
| 服务返回 404 | 选中码该地区无组价数据 | 正常缺口，如实转达 |
| 服务返回 503 | 知识服务 :8100 / LLM :8099 不可达 | 起知识服务 / 查 :8099 |
| 服务返回 502 | LLM 选码输出非合法 JSON | 重试或查 :8099 |
| `decision 不是合法 JSON` | resume 的 `--decision` 不是合法 JSON | 按闸类型模板重新构造 decision |
| HITL `status=未就绪` | 传了 `--spec 2013`（组价数据未就绪） | 预期，要算价用 2024 |

## 使用原则（速查）

1. 版本不猜 → 先 `ask_clarification`；描述不足也先反问
2. HITL 模式：你只「起会话 + 把 cost-hitl marker 原样贴进回复」，**不逐闸 resume、不替用户做闸内决策**（红线 5）——闸交互由内嵌控件 + 用户完成
3. marker 代码块**一字不改**地放进回复（别翻译/别改格式），否则前端识别不出、控件不出现
4. 费率/税率等政策数**用用户给的值，不填默认**（红线 4）——即便走命令行兜底也一样
5. `need_review` / `no_source` / `blocked` 如实透传，不杜撰、不当定稿
6. compose 一次性模式不算钱；要算到总造价走 HITL（start）
