---
name: cost-agent
description: 算量计价 CostAgent 技能。接收构件/做法的自然语言描述做组价。两种模式：① 一次性「选码+组价取数」（compose，快速查清单码/取数）；② 可中断 HITL 完整组价流程（start/resume，逐闸：编码→定额→信息价→费率→参数→总造价复核，每个需人介入点用 ask_clarification 停下来等用户确认/录入，最后出总造价）。覆盖深圳房建组价（清单计量国标 GB 50854 2013/2024 双版隔离）。适用于"C30现浇矩形柱组价""走完整组价流程算到总造价""某砌体墙套什么清单码"。强制：版本不猜先反问、选码只在候选内不造码、低置信转人工、缺价不杜撰、费率/税率等政策数由用户给不替填默认。
---

# 算量计价 CostAgent（cost-agent）

## ⛔ 红线（最优先，先于一切回答）

1. **版本不猜，先反问**：用户没明确说按哪版国标（2013 / 2024）组价时，**必须先 `ask_clarification` 反问**，绝不替猜。2013/2024 同 9 位码不同义，**版本错 = 串库 = 给错编码与价格**。
2. **描述不足也先反问**：构件描述不足以选码（如只说"柱"未说混凝土等级/现浇预制）时，先 `ask_clarification` 补全关键特征。
3. **选码不造码、缺价不杜撰**：只接受脚本返回候选内的编码；`need_review` / 低置信时如实告知"需人工复核"，不当定稿；`no_source` 如实透传，不补编价格。
4. **费率/税率等政策数由用户给，不替填默认**：管理费率/利润率/取费基数/措施费/规费/税率等是工程政策参数，HITL 闸会停下来问用户——**必须把用户给的值原样填进 decision，绝不自己编一个默认值**。
5. **流程编排在服务端的图里，agent 不当编排器**：你（agent）只做「把闸呈现给用户 + 收集决策 + 调 resume 续跑」，**不要自己决定跳过某个闸、不要替用户做闸内决策**。是否停闸、下一步走哪由服务端的图决定。

---

## 两种模式怎么选

| 模式 | 用 | 何时 |
|---|---|---|
| **一次性 compose** | `cost.py compose` | 用户只想「知道某构件的清单码 / 工料机取数」，不需要走完整算价、不需要人逐步确认 |
| **HITL 流程 start/resume** | `cost.py start` → 逐闸 `resume` | 用户要「走完整组价、算到总造价、要人确认编码/录入费率」——可中断、可审计 |

服务地址默认 `http://localhost:8101`（环境变量 `COST_AGENT_URL` 覆盖）。脚本是纯 stdlib 薄客户端，沙箱内零依赖。

---

## 模式一：一次性组价（compose）

```bash
python3 /mnt/skills/public/cost-agent/cost.py compose \
  --description "C30现浇混凝土矩形柱" --spec 2024 --region 深圳
```

返回 `{selection{code,confidence,need_review,...}, code, price{工料机+信息价}, price_status}`。
- `need_review=true` / `code=null` → 转人工复核，不当定稿。
- `price` 内 `no_source` 资源 = 缺信息价缺口，如实转达不补编。
- compose **不算钱**（不出综合单价/总造价）；要算到总造价走 HITL 模式。

---

## 模式二：可中断 HITL 完整组价（start / resume）—— 对话驱动

**核心循环**：`start` 拿到第一个闸 → 把闸呈现给用户、用 `ask_clarification` 收他的决策 → 把决策转成 JSON 调 `resume` → 拿到下一个闸 → 重复，直到 `status=done`（出总造价）或 `blocked`。

### 步骤 0：先确认版本（红线 1），再起会话

```bash
python3 /mnt/skills/public/cost-agent/cost.py start \
  --description "C30现浇矩形柱" --spec 2024 --region 深圳
```

返回精简视图：
```json
{"task_id": "abc123...", "status": "awaiting_input", "gate": { ...闸 payload... }}
```
**记住 `task_id`**——后面每次 `resume` 都要用同一个（它在你这次 start 的输出里，务必回看）。

### 步骤 1：循环——按 `gate.gate_type` 呈现闸 + 收决策 + resume

读 `gate.gate_type`，分三类处理。**每类都先用 `ask_clarification` 把闸内容如实呈现给用户**（带依据，不替用户决定），拿到用户回答后转成 decision JSON，调：

```bash
python3 /mnt/skills/public/cost-agent/cost.py resume \
  --task <task_id> --decision '<decision JSON>'
```

resume 返回的又是 `{task_id, status, gate}`——`status` 还是 `awaiting_input` 就继续下一闸，`done` 就到步骤 2。

#### 闸类型 A：`confirm`（编码 / 定额）

`gate` 含 `proposal`（系统建议值）、`evidence`（来源 + 置信度，**务必转达给用户做判断依据**）、`alternatives`（候选内备选）。
用 `ask_clarification` 问用户：通过 / 选哪个备选 / 还是手动改。decision 三选一：

| 用户意思 | decision JSON |
|---|---|
| 认可系统建议 | `{"action":"approve"}` |
| 选某个备选 | `{"action":"select_alternative","value":"<alternatives 里的 code>"}` |
| 手动改成别的 | `{"action":"manual_override","value":"<用户给的编码/子目号>"}` |

#### 闸类型 B：`input`（setup / 缺价录入 / 费率 / 参数）

`gate` 含 `fields`（每项 `{key,type,label,options?,required?}`），可能含 `context`（如缺价材料的名称/规格/消耗量，呈现给用户帮他报价）。
用 `ask_clarification` 把每个字段问清楚（`type=enum` 给出 `options` 让用户选；`required` 的必须有值），decision = **字段 key→用户给的值** 的 dict：

- 缺价录入（单字段）：`{"value": 5.5}`（用户报的单价）
- 综合单价费率：`{"management_fee_rate": 10, "profit_rate": 5, "risk_rate": 0, "fee_base": "labor_machine"}`
- 项目级参数：`{"measure_fee": 1000, "other_fee": 0, "fee_levy": 500, "tax_rate": 9}`

> ⛔ 红线 4：费率/税率这些**必须用用户给的值**。用户没给就继续 `ask_clarification` 追问，**绝不自己填默认**。

#### 闸类型 C：`review`（总造价末尾复核）

`gate.rollup` 是总造价明细（分部分项/措施/规费/税金/总造价）。呈现给用户复核，确认无误后：
```json
{"action":"approve"}
```

### 步骤 2：终态

- `status=done`：返回里有 `rollup`（总造价明细）+ `audit_count`/`override_count`。把总造价 + 关键明细呈现给用户，说明哪些是用户录入/确认的（可审计）。
- `status=blocked`：选不出码且用户也没给值 → 如实告知"未能定编码，需人工处理"，不硬编。

### 完整一轮示例（对话节奏）

1. 用户：「帮我把 C30 现浇矩形柱按 2024 走完整组价算到总造价」
2. agent：`cost.py start -d "C30现浇矩形柱" --spec 2024` → 拿到 task_id + 编码 confirm 闸
3. agent：`ask_clarification`「系统建议编码 010502006（依据：…，置信 …），有备选 …。通过/选备选/手改？」
4. 用户：「就用 010502006」→ agent：`resume --task <id> --decision '{"action":"approve"}'` → 定额闸
5. …（定额 confirm → 缺价 input 逐条 → 费率 input → 参数 input → 总造价 review）…
6. `status=done` → agent 呈现总造价明细 + 审计说明

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

1. 版本不猜 → 先 `ask_clarification`
2. HITL 模式：你只「呈现闸 + 收决策 + resume」，**不替用户做闸内决策、不跳闸**（红线 5）
3. confirm 闸务必把 `evidence`（来源/置信）转达用户做判断
4. input 闸的费率/税率**用用户给的值，不填默认**（红线 4）
5. `need_review` / `no_source` / `blocked` 如实透传，不杜撰、不当定稿
6. 全程用同一个 `task_id`
