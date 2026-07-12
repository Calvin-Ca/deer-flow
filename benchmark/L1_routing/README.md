# L1 路由层评测（lead agent「接到活先干什么」）

> 量的是编排层第一跳：一条用户请求进来，lead agent **该调能力时是不是真去调了**（而非凭记忆瞎答）、
> **该反问时是不是先 `ask_clarification`**、**该拒答时是不是体面拒了**。答案质量、检索召回、终态
> 正确性都不在本层——分别见 L3/L6。
>
> 数据集按「**维护方式（手工主池 / 机器生成专项）**」分文件，场景与难度不做文件边界、只做
> 用例标签（`capability` × `difficulty`），出报告按标签切片。字段定义见 `data/README.md`。

---

## 1. 判定口径：看行为轨迹，不看自述

现役架构里路由是 lead agent 智能的一部分：query 直接进 lead（Qwen3-8B + 系统提示词 + 工具面），
由模型自己决定调哪个工具，没有独立的路由代码。

判法（`run_routing_experiment.py`，进程内嵌入式 DeerFlowClient）：agent 跑完后翻它的**工具调用
序列**——调用里有没有出现 `ROUTE_TOOL_NAMES` 集合内的名字（`cost_workflow_start/node/resume/state`、
`verify_bill_code`、`cost_calc`、`task`）→ 得出 `did_route`；有没有调 `ask_clarification` →
得出 `did_clarify`。两个观测值对金标的 `expect_route`/`expect_clarify` 逐条判分（本地 Python
判官，Langfuse 只当账本）。**不关心模型心里把意图分成哪类，只看手最终伸向了哪个工具。**

---

## 2. 数据集清单

| 文件 | 维护方式 | 条数 | 内容 | Langfuse dataset |
|---|---|---|---|---|
| `data/user_requests.jsonl` | 手工主池 | 78 | 路由主池，覆盖全部能力：strong 54 + colloquial 24（「这道墙砌起来得多少钱」类无关键词问法） | `user-requests-routing` |
| `data/bill_match_routing.jsonl` | 机器生成专项 | 90 | 清单选码/核实单意图深测（difficulty=real_paste）：真实 2013 清单行整段特征粘贴，零编造，`gen_bill_match_routing.py` 幂等重生成（详见 §5） | `bill-match-routing` |

两个 dataset 独立出分不混跑：主池管**全能力覆盖与 variant 横比**，专项集管**单意图深测**
（机器重生成会整体覆盖，故不并入主池）。id 无冲突，需要全量口径时可 union。

**沿革（2026-07-11 整并，细节见 git 历史）**：早期曾有多份分立数据集——AI 生成的「冻结金标」
`agent_routing_eval.jsonl`（34 条，旧需求产物）、口语扩充 `user_requests_colloquial.jsonl`、
已退役独立意图路由器的评测集 `intent_fallback_eval.jsonl`（其金标字段 `expect_confidence` 等
是给那台机器的零件断言，与本层行为判定不通用）。均已按现行六能力需求逐条审校、转标并入主池：
可用用例重打标签保留，listing 列清单用例随需求删除，逐字/近逐字重复去重，会话粘性用例
（多轮行为，单轮 runner 判不了）归 `L6_agent/trajectory/`。旧 Langfuse dataset
`agent-routing-eval` 停用，历史 runs 保留可查。

**当前没有冻结基线**：主池随需求演化，跨数据版本的分数不可纵比（同版本内 variant 横比不受影响），
数据变更以 git 历史为准。**基线策略**：待六能力需求与数据双稳后，从主池拍快照封存一代冻结基线
作回归护栏（基线换代而非漂移）。

---

## 3. 指标与判定

设 R = `expect_route=true` 的用例集。

1. **路由率** = (R 中真调了 `ROUTE_TOOL_NAMES` 内工具的条数) / |R| ——弱模型能否识别并调用能力而非自己瞎答。
2. **口径遵守率（主判据）**：`group=no_version` 全部用例（norm/cost 两侧统一，2026-07-11 起）——
   缺版本/地区**不反问**、默认深圳·2013 直接执行、回复中带口径声明（反问=违例）。agent 定位即
   深圳市房建组价助手，版本/地区是既定口径不是待澄清信息；`ask_clarification` 只留给实质信息
   不足（特征/清单内容/计算参数）。
3. **出界告知率**（`group=out_of_scope`）：他省口径体面告知，不取数、不拿深圳数据冒充（EH-03）。

（原第 4 项「联网兜底呈现合规率」已随能力裁撤删除，2026-07-12：联网兜底代码随旧任务层退役后
未重建,产品裁定不再提供——零召回的正确行为统一为如实拒答给出路,原 web_fallback 用例已并入
`boundary` 组。）

`ask_clarification` 触发 HITL 属**红线单列**（门 0.95），不并入路由分。建议初判阈值：路由率 ≥ 0.8、
红线遵守率 ≥ 0.95（安全攸关从严），最终由用户结合首轮跑分确认。

**2024 版已裁出产品范围（2026-07-11 用户裁定，系统仅深圳·2013）**：点名 2024 的请求与他省口径
同等处理——不取数、不拿 2013 数据冒充、体面告知（`group=out_of_scope`）。主池原 36 条 2024 问法
留 4 条转标为版本出界护栏（A3/A18/B3/B12），其余删除；专项集的 10 条 2024 金标随之移除（90 条）。
今后扩充用例（c5 计算/c6 整单等）一律按深圳·2013 口径造。

**归因三分法**（失败用例先翻 Langfuse trace 再动手）：测量错（`ROUTE_TOOL_NAMES` 漏真实工具名）/
服务没起（MCP 依赖）/ agent 真错（这时才调提示词或换基座）。

---

## 4. 能力覆盖对账（对 CLAUDE.md §1 六能力，2026-07-11 盘点）

| 能力 | capability | 现有覆盖 | 缺口 |
|---|---|---|---|
| 1 规范问答 | `c1_norm_qa` | 主池 19（含口语 4）：GB50500/50854-2013 条文、缺版本默认口径、boundary（未收录→拒答给出路）、2024/安装出界护栏 | — |
| 2 清单智能匹配 | `c2_bill_code` | **专项 90**（零编造真实 2013 特征，选码 78 + 编码/特征核实 12）+ 主池 2 | — |
| 3 定额方案推荐 | `c3_quota` | 仅 B37 一条（且是缺特征反问用例） | **缺**「已编好的清单项→只要定额推荐」问法 |
| 4 智能询价 | `c4_price` | 主池 13（含口语 6）：材料+规格+期号取数、趋势、他省越界 | — |
| 5 组价自动计算 | `c5_calc` | 仅 D1/D2/D4/CC12 核对类 4 条 | **缺**纯计算请求（给量/含量/单价求合价等），`cost_calc` 路由出口无分可算 |
| 6 整单组价闭环 | `c6_full_costing` | 单构件组价 34（no_version、no_feature、out_of_scope、compound） | **缺**多行清单「整单组价」问法（终态质量归 L6 cost_task，但 L1 的整单路由面没测） |

补齐顺序（走专项集，照 `bill_match_routing` 模板锚定真实数据）：① c5 纯计算请求
10~20 条；② c6 多行清单整单组价 5~10 条；③ c3「清单项→定额推荐」问法变体若干。

---

## 5. 清单匹配专项集 `bill_match_routing.jsonl`（90 条，零编造）

专测**清单选码/核实这一种意图**的路由：造价员给出项目特征要选码/核实编码时，lead 是否路由到能力
（而非凭记忆答码——串库红线）、是否遵守「缺版本不反问、按深圳·2013」口径策略。

- **零编造**：90 条 query 的项目特征全文逐字取自 `../L3_retrieval/data/match_gold_2013.jsonl`
  （91 条真实项目清单行，按 query 去重后 90），只套「对话框问法」模板（确定性轮转非随机）；
  核实问句里的编码也是该行真实金码。每条 `note` 带源文件行号可溯源。2024 金标源已随产品范围
  裁定移除（2026-07-11）。
- **生成**：`python data/gen_bill_match_routing.py`（幂等覆盖；金标增补后重跑扩容，勿与已跑基线混比）。
- **分组**：`no_version` 60（默认深圳·2013 口径，反问=违例）/ `with_version` 18（问法带 2013）/
  `code_check` 12（带真实金码问「对不对/特征有漏吗」，期望 `verify_bill_code` 或 task 派
  cost-agent）。全部 `expect_route=true`、`expect_clarify=false`。
- **与 `clist-match-eval` 的分工**：那边量**检索召回**（描述→金码命中率，L3 知识层）；这边只量
  **路由**（该调工具时真调了没，L1 编排层）——同一批真实特征，两层各测各的。

---

## 6. 怎么跑（服务器）

```bash
# 灌金标（按集）
uv run --project backend python benchmark/_shared/upload_datasets.py --only user_requests    # 主池 78（含口语 24）
uv run --project backend python benchmark/_shared/upload_datasets.py --only bill_match_routing  # 专项 90

# 批量出分（逐 variant 换 --run-name，Langfuse Datasets→Runs→Compare 横向比）
uv run --project backend python benchmark/L1_routing/run_routing_experiment.py --run-name <variant> [--model qwen-plus]   # 缺省跑主池 user-requests-routing
uv run --project backend python benchmark/L1_routing/run_routing_experiment.py --dataset bill-match-routing --run-name <variant>
```

改动后的标准闭环（详见根 CLAUDE.md §4.1）：①F5 debug 快验典型 query → ②runner 批量出两率 →
③失败用例翻 Langfuse trace 归因（测量错/服务没起/agent 真错三分），需单条复现或 clarify 续跑再动
`_shared/probe_gateway.py`。
