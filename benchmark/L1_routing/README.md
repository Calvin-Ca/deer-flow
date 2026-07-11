# Agent 集成评测集（方案 0 skill-only 升级判定门）

> 用途：跑 **AGENT_INTEGRATION_DEV.md §0** 的「升级判定门」——在**默认 lead agent（skill-only，agent_name=None）**下逐条提问，量两项指标，决定是否从方案 0 升级方案 A。
> 评测口径：在 **flash / thinking / pro** 三档下做（避开 ultra 的 task 委派双脑歧义，见 DEV §1.2-5）。

## 评测集 `agent_routing_eval.jsonl`

每行一个用例，字段：

| 字段 | 含义 |
|---|---|
| `id` | 用例号（对应 `ce-services/前端测试用例.md` 的 A*/B*；C*=价格 FR-I、D*=核对 FR-C 为 T9 后新增） |
| `agent` | 期望命中的能力：`norm-qa`（规范问答）/ `cost-agent`（算量计价）/ `price`（价格取数）/ `cost-check`（清单核对） |
| `group` | `no_version`（不带版本）/ `no_feature`（特征缺）/ `with_version`（带版本）/ `boundary`（越界拒答）/ `out_of_scope`（EH-03 他省口径体面告知）/ `web_fallback`（FR-K07 联网兜底）/ `session_sticky`（EH-05 会话粘性）/ `context_check`（FR-C）等 |
| `query` | 喂给对话框的原始问法 |
| `expect_route` | 是否**应该去调能力**（脚本/MCP 工具）。`boundary`/`out_of_scope` 用例为 `false`（应拒答/体面告知，不取数） |
| `expect_clarify` | 是否**应该先 `ask_clarification` 反问**。**T9-1 后口径分侧**：norm 缺口径→会话内**首次** `true`（EH-05）；cost 缺版本→`false`（默认深圳·2013，不反问）；cost 缺**特征**→`true`（EH-04，只问特征） |
| `gold` | 应使用的 standard/spec（cost 缺版本时为默认 `2013`）；越界/出界用例为 `null` |
| `note` | 判读提示 / 已知召回缺口归因 |

## 指标（§0 升级判定门 + T9 口径策略回归）

设 R = `expect_route=true` 的用例集。

1. **路由率** = (R 中模型**真去调了对应能力** 的条数) / |R|
   - 衡量弱模型能否正确识别并调用能力，而非自己瞎答。
2. **口径红线遵守率（主判据，安全攸关，分侧）**：
   - **cost 不反问率**（`group=no_version` 的 B 组）：缺版本**不反问**、默认深圳·2013 且首答带口径声明的比例（T9-1 行为反转后的新红线；反问=违例）；
   - **norm 首次反问率**（`group=no_version` 的 A 组）：会话首次缺口径真反问的比例；
   - **会话粘性达成率**（`group=session_sticky`）：同会话第二问**不再反问**的比例（EH-05）。
3. **出界告知率**（`group=out_of_scope`）：他省口径体面告知（不取数、不给深圳数据冒充）的比例（EH-03/C-02）。
4. **联网兜底呈现合规率**（`group=web_fallback`）：降级标注头 + URL/访问日期完整保留的比例（FR-K07 Tier-2）。

## 判定（§0）

- 两项**均达标** → **停在方案 0**，不做 A/D。
- **红线遵守率不达标** → 升级**方案 A**（常驻 `SOUL.md` 直接补强红线；方案 D 治不到此病根）。
- 仅当瓶颈是**上下文隔离 / 并行重取数**（非红线）→ 才考虑方案 D。

> 达标阈值未在 DEV 文档拍死，建议初判：路由率 ≥ 0.8、红线遵守率 ≥ 0.95（安全攸关从严）。最终阈值由用户结合首轮跑分确认。

## 怎么跑

当前为**人工判读**：四服务起齐（见 `前端测试用例.md` 前置表，:8099/:8100/:8101 + Gateway），在前端对话框逐条贴 `query`，对照 `expect_route` / `expect_clarify` 记录命中，按上面公式算两率。

> 归因速查见 `前端测试用例.md` 第三节：答非所问/召回错构件多为**知识层 ce-code 召回缺口**（非编排 bug）；不反问/不调脚本才是**编排层**问题（调 prompt 或切 qwen-plus 基座）。

---

## 兜底层评测集 `intent_fallback_eval.jsonl`（意图混合路由 M1）

`agent_routing_eval.jsonl` 是**确定性层**金标（强信号必分对，不动，作回归护栏）。本集专测**LLM 兜底层**——
关键词穷举疲劳 + 口语变体漏判的难例/含糊集，验「确定性判低置信 → 32b 兜底捞回」这条混合路由腿。

每行字段：`id`（F*=难例 low 组 / C_F*=强信号 high 控制组）、`query`、`group`
（`colloquial_cost`/`colloquial_price`/`generic_norm`/`colloquial_oos`/`strong_ctrl`）、
`expect_confidence`（`low`=确定性该升级走兜底 / `high`=强信号直配不动）、`gold_capability`（正确能力落点）、`note`。

**三项指标**（跑 `cd ce-services && uv run python -m tools.intent_fallback_eval [--llm]`）：
1. **升级门正确率**（离线）：`route().route_confidence` 是否等于 `expect_confidence`（该升的升、不该升的不动）。
2. **强信号控制组直配正确率**（离线）：`high` 组 `route().capability` 是否等于金标（证零延迟直配没判错）。
3. **LLM 兜底准确率 + 延迟**（`--llm` 真调 32b）：`low` 组 `classify_intent` 对金标能力的准确率 + 均值/P95 耗时（对齐 FR-K ≤3s）。

⚠️ 本集**升级率≈70% 是刻意的**（难例集富集含糊/口语样本，用于压测兜底腿）——**非真实流量升级率**；
真实流量绝大多数命中强信号走确定性直配（零延迟），兜底只是少数长尾。红线闸（EH-03 出界 / caliber）
两条路都确定性、LLM 不碰（`F14` 专验：口语他省仍由确定性重推识别出「北京」出界）。

---

## 真实用户请求扩充集（2026-07-09 新增，不动上面两个冻结金标）

上面 `agent_routing_eval.jsonl` / `intent_fallback_eval.jsonl` 是**冻结回归护栏**（改动会污染基线）。下面两个是**同 schema 的真实请求扩充集**——按「造价员在前端对话框实际会打的话」造，覆盖全部落点类型，用真实项目特征（混凝土强度/截面/砂浆等级/砖规格/卷材层数…）而非占位符。**id 延续上面编号，可与冻结集直接 union**。

| 文件 | schema | 内容 | 跑法 |
|---|---|---|---|
| `user_requests.jsonl` | 同 `agent_routing_eval.jsonl`（`agent`/`group`/`query`/`expect_route`/`expect_clarify`/`gold`/`note`） | 强信号真实请求，覆盖 cost-agent（组价 with/no_version、EH-04 缺特征、listing 列清单、EH-03 跨省、EH-01 比选/复合）、norm-qa（GB50854 计量 / GB50500 计价 / GB50856 安装 with/no_version、boundary 未收录、web_fallback）、price（信息价/趋势/跨省）、cost-check（FR-C 核对）、out-of-domain | 同上：前端对话框逐条贴 `query`，按路由率 + 口径红线遵守率人工判读 |
| `user_requests_colloquial.jsonl` | 同 `intent_fallback_eval.jsonl`（`id`/`query`/`group`/`expect_confidence`/`gold_capability`/`note`） | 口语变体难例，压测 LLM 兜底腿：`colloquial_cost`/`colloquial_price`/`generic_norm`/`colloquial_check`（新增，FR-C 口语核对）/`colloquial_oos`（红线走确定性重推） | `cd ce-services && uv run python -m tools.intent_fallback_eval [--llm]`，同兜底三指标 |

> 与 §8 无专家版口径一致：这批是**方法3 合成**的 input（真实分布近似、非真实流量），标签由 §4.3 决策表 / T9-1 口径策略机器可推导；`gold` 为**待跑分验证的期望值**，非专家金标，评测报告须声明「非真实流量分布」。新增构件的组价终态（`expected_quota`/`fee_band`）另见 `../L6_agent/cost_task/`。

---

## 清单匹配意图路由集 `bill_match_routing.jsonl`（2026-07-11 新增，100 条，零编造）

专测**清单匹配这一种意图**的路由：造价员给出项目特征要选码/核实编码时，lead 是否路由到能力（而非凭记忆答码）、是否遵守「cost 侧缺版本不反问、默认深圳·2013」口径策略（T9-1，非安全红线）。

- **零编造**：100 条 query 的项目特征全文**逐字取自** `../L3_retrieval/data/match_gold_2013.jsonl`（91 条真实项目清单行，按 query 去重后 90）与 `../L3_retrieval/data/match_gold.jsonl`（10 条 2024），只套「对话框问法」模板（确定性轮转，非随机）；核实类问句里的编码也是该行**真实金码**。每条 `note` 带源文件行号 + 金码可溯源。
- **生成**：`python data/gen_bill_match_routing.py`（幂等覆盖；金标增补后重跑扩容，勿与已跑基线混比）。
- **schema** 同 `agent_routing_eval.jsonl`，id M1~M100，可 union。分组：`no_version` 60（选码不带版本，gold=2013 默认口径策略，反问=违例）/ `with_version` 28（18 条带 2013 + 10 条 2024 金标必须带版本）/ `code_check` 12（agent=cost-check，带真实金码问「对不对/特征有漏吗」，期望 `verify_bill_code` 或 task 派 cost-agent）。全部 `expect_route=true`、`expect_clarify=false`。
- **跑法**（服务器）：`uv run --project backend python benchmark/_shared/upload_datasets.py --only bill_match_routing` 灌库，再 `uv run --project backend python benchmark/L1_routing/run_routing_experiment.py --dataset bill-match-routing --run-name <variant>`。独立 dataset（`bill-match-routing`），不并入冻结基线。
- **与 `clist-match-eval` 的分工**：那边量**检索召回**（描述→金码命中率，L2 知识层）；这边只量**路由**（该调工具时真调了没，L1 编排层）——同一批真实特征，两层各测各的。
