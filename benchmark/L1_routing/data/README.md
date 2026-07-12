# L1 路由评测数据集：字段说明

> 本目录两个 jsonl 同 schema，每行一个用例。评测口径与指标见上层 `../README.md`，
> 本文只讲**每个字段是什么、怎么标**。

## 文件一览

| 文件 | 条数 | 维护方式 | Langfuse dataset |
|---|---|---|---|
| `user_requests.jsonl` | 78 | 手工维护（增删改直接编辑，随需求演化） | `user-requests-routing` |
| `bill_match_routing.jsonl` | 90 | **勿手改**——`gen_bill_match_routing.py` 幂等重生成整体覆盖 | `bill-match-routing` |

改完数据后灌库：`uv run --project backend python benchmark/_shared/upload_datasets.py --only user_requests`（或 `bill_match_routing`）。同 id 幂等覆盖；**已删用例不会自动从 Langfuse 消失**，需 UI 手动 archive。

## 字段定义

```json
{"id": "B31", "agent": "cost-agent", "capability": "c6_full_costing", "group": "no_version",
 "difficulty": "strong", "query": "C30现浇混凝土矩形柱怎么组价？截面500×500，泵送",
 "expect_route": true, "expect_clarify": false, "note": "……"}
```

> 曾有 `gold`（应采用的口径）字段——2013 成为系统唯一口径后其取值可由 capability/group 完全
> 推导（无信息量、runner 也不读），2026-07-11 已删除。

### `id` — 用例号

唯一、不复用。前缀即来源批次：`A*` 规范问答 / `B*` 组价 / `C*` 价格 / `D*` 核对 /
`P*` 复合 / `G*` 域外 / `CC*`、`F*`、`C_F*` 口语难例（历史批次转标并入）/ `M*` 选码专项
（机器生成）。新增用例续用能力对应前缀的下一个号即可。

### `agent` — 期望能力落点（旧四分类，保留兼容）

`norm-qa` 规范问答 / `cost-agent` 组价 / `price` 价格 / `cost-check` 核对 / `out-of-domain` 域外。
历史字段，早于六能力表；判分不读它，留作旧报告对照。**新用例照常填，但切片分析用 `capability`。**

### `capability` — 六能力标签（对齐根 CLAUDE.md §1）

| 值 | 能力 |
|---|---|
| `c1_norm_qa` | 1 规范问答助手 |
| `c2_bill_code` | 2 清单智能匹配（选码 + 特征完整性核实） |
| `c3_quota` | 3 定额方案推荐 |
| `c4_price` | 4 智能询价（深圳信息价取数/趋势） |
| `c5_calc` | 5 组价自动计算（工程量/含量/单价/合价的计算与核对） |
| `c6_full_costing` | 6 整单组价全闭环（含单构件端到端组价） |
| `out_of_domain` | 域外（非造价请求） |

**按用户诉求类型标，不按期望行为标**：越界的组价请求（「按北京定额组价」）仍标
`c6_full_costing`——诉求是组价，越界性体现在 `group=out_of_scope`。

### `difficulty` — 信号强度三档（L1 的难度轴，与场景正交）

| 值 | 含义 | 例 |
|---|---|---|
| `strong` | 明确问法：带规范号/版本/动作词 | 「按2024国标给"C30矩形柱…"组价」 |
| `colloquial` | 口语无关键词，意图靠理解推断 | 「这道墙砌起来得多少钱」 |
| `real_paste` | 真实清单行整段特征粘贴（数百字） | M1 锚索 14 项特征全文+「套什么清单码？」 |

按 query 本身的信号强度标，与能力无关——每个能力都可以有三档问法。

### `group` — 行为分组（决定该条落进哪个指标的分母）

| 值 | 场景 | 期望行为 |
|---|---|---|
| `with_version` | 带 2013 版本/规范号 | 直接执行，不反问（2024 版已裁出产品范围，点名 2024 的用例归 `out_of_scope`） |
| `no_version` | 缺版本/地区 | **不反问**（norm/cost 两侧统一，2026-07-11 起）：按深圳·2013 直接执行 + 回复中口径声明（反问=违例） |
| `no_feature` | 构件特征不足 | 只反问特征，不问版本（EH-04） |
| `with_material` / `trend` | 询价：指定材料 / 多期走势 | 取数/算价差，不反问 |
| `context_check` / `code_check` | 核对清单行 / 核实编码 | 确定性核对（`verify_bill_code` 等） |
| `compound` | 比选/跨能力复合 | 拆子任务分派，不糊成一条 |
| `boundary` | 点名未收录规范（如 GB50016/GB50011） | 照调检索→零召回→**如实拒答给出路**（说明已查范围+建议渠道），不编条文、不联网（联网兜底已裁撤 2026-07-12） |
| `out_of_scope` | 口径出界：他省 / 点名 2024 版 / 安装规范 | 不取数、不反问、**不拿库内 2013 数据冒充作答**，体面告知仅支持深圳·2013 房建（EH-03；2024 出界系 2026-07-11 产品范围裁定） |
| `out_of_domain` | 非造价请求 | 不调工具，声明能力范围 |

### `query` — 原始问法

喂给对话框的原文，一字不改。专项集的项目特征逐字锚定 `../../L3_retrieval/data/match_gold*.jsonl`
真值（零编造），核实问句里的编码也是该行真实金码。

### `expect_route` — 是否应调能力（布尔）

判定口径：agent 的工具调用序列命中 `ROUTE_TOOL_NAMES`（`cost_workflow_start/node/resume/state`、
`verify_bill_code`、`cost_calc`、`task`，见 `../run_routing_experiment.py`）即 `did_route=true`。
`out_of_scope`/`out_of_domain` 用例为 `false`（正确行为是拒答/告知，调了取数工具反而违规）。

### `expect_clarify` — 是否应先反问（布尔）

判定口径：命中 `ask_clarification` 工具即 `did_clarify=true`。打标原则（2026-07-11 起统一）：
**版本/地区缺失→`false`**（agent 定位即深圳·2013 默认口径，反问=违例）；**实质信息缺失→`true`**
（构件特征、清单内容、计算参数、问法歧义）。
注意 `expect_clarify=true` 的用例会在反问处中断（HITL），当轮不再路由——runner 对这类条目
不挂路由分（不计分≠0 分）。

### `note` — 判读提示 / 溯源

写清「为什么这么标」：期望行为的依据（EH-**/FR-**/T9-1 条款号）、金码与源文件行号（专项集）、
易混淆点（如「问砌墙钱是组价不是询价」）。归因失败用例时先读它。
