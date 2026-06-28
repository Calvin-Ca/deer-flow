# lead_agent 意图识别与路由｜方案记录

| 项目 | 内容 |
|---|---|
| 状态 | **方案 A 已落地（2026-06-28）** + 规范版本澄清已下沉到各能力块「版本闸门」；B/C 待选型 |
| 范围 | lead_agent 入口层：判别用户意图（规范问答 / 算量组价 / …）并路由到对应脚本 |
| 关联代码 | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`（`SYSTEM_PROMPT_TEMPLATE`、`apply_prompt_template`）；`ce-services/norm`（qa.py 对应能力）、`ce-services/cost`（cost.py 对应能力） |
| 记录日期 | 2026-06-28 |

---

## 背景与现状

- **deer-flow 原生无专门意图识别模块**。当前这套是重写后的单 agent（ReAct）harness，意图识别是**隐式**的：LLM 在 tool-calling 循环里，靠 `SYSTEM_PROMPT_TEMPLATE` + 工具/技能描述自己判，自己选 `bash qa.py`（规范）还是 `bash cost.py`（组价）。源码里 `intent/classif/router` 的命中全是同名不同事（bash 命令安全分级、异常分级、FastAPI 路由），不是用户意图识别。
- 真正要路由的就 **norm（规范条文问答）vs cost（算量组价）** 两条道；两侧触发词在造价领域几乎不重叠，判别属窄域术语判别。
- 约束：lead_agent 跑 **Qwen3-8B**，function-calling / 自由决策不稳；让它多做一步"先分类"等于押注其弱项。
- **关键非对称性**：路由错的代价 ≪ 版本（2013/2024）错的代价。路由分错只是调错脚本、返回空/不相关，模型可重试；版本串库是 prompt 标"最高红线"的事（给错编码/条文/价格）。结论：**意图路由可大胆用规则；版本闸门必须保守，继续走 `ask_clarification` 反问，绝不规则猜默认。** 两件事必须解耦。

## 意图类别（5 类，非二分）

| 意图 | 路由到 | 说明 |
|---|---|---|
| `norm` 规范条文问答 | `qa.py` | 计量规则/项目特征/综合单价构成等 |
| `cost` 算量组价 | `cost.py` | 构件→9 位清单码→工料机取数 |
| `clarify` 信息不足 | `ask_clarification` | 缺版本、构件描述不足（语义判断，规则做不了，交现有澄清流程） |
| `both` 复合意图 | 串行：先 norm 再 cost（或反序） | "这墙怎么计量、顺便组个价" |
| `chat` 能力外/元对话 | 直接回答 | 闲聊、问能力、问历史 |

---

## 方案 A：prompt 内显式两段式路由（首选，零代码）✅ 已落地

> **落地记录（2026-06-28）**：已写入 `prompt.py` 的 `SYSTEM_PROMPT_TEMPLATE`：
> - `<safety_redline>` 与 `<skill_runbook>` 之间新增 `<routing priority="高">` 块（5 类主意图，先分类后执行）；
> - **版本澄清下沉**：`<safety_redline>` 不再写"必须先问版本"，改为指针；norm / cost 两条能力块各自新增「版本闸门」承载 `ask_clarification` 澄清（standard 代号 / spec 必填、无默认、绝不猜）；
> - `<workflow>` 改为"先分类后澄清"。
> - 回归测试：`backend/tests/test_lead_agent_prompt.py::test_default_template_embeds_routing_block`、`::test_version_clarification_is_pushed_down_to_per_capability_gate`（全文件 25 passed）。
> - 注意：routing 块内 `{{ norm | cost | both | clarify | chat }}` 必须双花括号转义，否则 `.format()` 抛 KeyError。


在 `SYSTEM_PROMPT_TEMPLATE` 的 `<skill_runbook>` **之前**插一个 `<routing>` 决策块，强制模型"先分类、再执行"，把现有隐式决策摆到台面。

- **机制**：纯 prompt，不加组件、不加延迟、不注册新工具（脚本本来走 bash）。
- **优点**：改动最小，与 deer-flow"prompt 驱动单 agent"范式一致；配 `lead_agent.system_prompt_path` 抽成独立模板文件 + Langfuse `variant` 标签可做 A/B 对比路由准确率。
- **缺点**：仍押注 Qwen3-8B 的判别能力，弱模型可能分错；不可稳定单测。
- **决策块草稿**：

```
<routing priority="高">
收到用户消息后，先在思考里完成意图分类，只能选一个主意图，再据此执行：
1. 主意图 ∈ { norm | cost | both | clarify | chat }
   - "怎么计量/按什么算量/项目特征/综合单价含哪些费用/规范怎么规定" → norm
   - "组价/套定额/套什么清单码/9位编码/工料机含量/信息价" → cost
   - 既问规则又要价 → both（先 norm 后 cost，分两步调脚本）
   - 给了构件/问题但缺规范版本或描述太含糊 → clarify
   - 问你是谁/能干啥/闲聊/对话历史 → chat，直接回答，不调脚本
2. 分类完，按 <skill_runbook> 对应能力执行；版本未定一律先 ask_clarification。
3. 拿不准 norm 还是 cost 时不要猜——用 ask_clarification 反问"查规范还是做组价"。
</routing>
```

## 方案 B：独立 router LLM（A 在 Qwen3-8B 上不够稳时升级）

把意图识别拆成一个**独立、输出受约束**的 LLM 调用——弱模型最可靠的形态：只吐小 JSON，不做工具选择。

- **输出契约**：`{"intent":"norm|cost|both|clarify|chat","spec_version":null|"2013"|"2024","missing":["spec","description"]}`
- **落地**：写 `before_model` 中间件（参考 `ClarificationMiddleware` 在链里拦截/改流向的写法），对首条用户消息分类一次。
  - 分类这步可单独用 **qwen-plus**（短、便宜，比让 8B function-call 稳），结果以 `<routing_decision>` 注入上下文；lead_agent 主体仍可保持 8B；
  - 或据分类结果给 lead_agent 注入**对应路由的精简 prompt**（norm 任务不塞 cost runbook，减干扰）。
- **优点**：路由可靠性显著高于自由 tool-call；可单测。
- **缺点**：多一次 LLM 调用 + 一道中间件；是 deer-flow 没有的增量改造。

## 方案 C：编码优先的分层 + 兜底（推荐主路线）

规则（纯代码）做主路由覆盖高置信大头，模糊尾巴才落到 LLM/澄清。

```
用户消息
  └─ 规则路由器(纯代码)  ← 命中高置信关键词/模式
       ├─ norm  → qa.py
       ├─ cost  → cost.py
       └─ 无把握/裸歧义 → 兜底：lead_agent(LLM) 自己判 或 ask_clarification 反问"查规范还是做组价"
```

- **机制**：确定性规则（正则/关键词 + 默认值，如"裸构件描述无疑问词→cost"）优先；只有规则拿不准才交给 LLM 兜底或反问。把 LLM 从"每条都判"降级成"只判模糊尾巴"。
- **优点**：零延迟/零 token、可解释、可 TDD（写 `test_intent_router.py` 断言路由），与项目"能确定就别交给模型/死命令"哲学一致；不给弱模型加失败面。
- **缺点**：纯规则覆盖不了裸描述歧义与"信息充分性"判断（故必须保留兜底，不能纯规则）；关键词表需维护。
- **前置量化**：落规则前先用 `ce-services/eval/` + `notebooks/` 评测集跑真实/构造 query，统计**意图歧义率**——若 85%+ 能高置信命中，编码路由明确划算；歧义率高则规则价值打折。该步是 ce-services 侧纯分析脚本，不碰 backend。

---

## 当前倾向

编码优先（**方案 C** 为主路线，LLM/澄清兜底），而非让弱模型每条都判。落地前先用 eval_set 量化意图歧义率确认规则覆盖率。版本（2013/2024）闸门独立、保守，始终走 ask_clarification。
