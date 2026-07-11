# 多轮轨迹评测（HITL 续跑 + 轨迹效率）

> 层归属：L6 任务级的多轮子集。单轮路由（L1）测不到的东西都在这——澄清之后接得好不好、多轮会不会打转。schema 见 `trajectory.jsonl`（`turns` 数组逐轮标 `assistant_expect.action` + `check`）。

## 测什么（两个盲区在此收口）

**① HITL 续跑质量**：`ask_clarification` 触发中断后，用户答复能否被正确利用——不重复问已给的、不丢前几轮攒下的特征、澄清等待中换话题能干净接管（TRAJ-01/02/05/07/08）。

**② 轨迹效率**：任务做成之外，花了多少步、有没有打转。每条 case 带 `budget` 预算：

| 字段 | 含义 |
|---|---|
| `max_steps` | 全轨迹工具调用步数上限（超了即低效，即便最终做对） |
| `max_same_tool_repeat` | 同一工具连续重复调用上限（超了=打转，弱模型典型失败） |
| `max_clarify_rounds` | 澄清轮数上限（0=本 case 不该反问；超了=澄清不收敛） |

## 指标

| 指标 | 口径 | 门线 |
|---|---|---|
| `resume_success` | 澄清后按 `assistant_expect` 推进的比例（逐 turn 判 `check`） | 回归基线 |
| `no_redundant_reask` | 不重复追问已给信息的比例（policy 同名项） | 回归基线 |
| 预算超限率 | 任一 `budget` 项超限的 case 比例（任务对但超预算单列，不糅进成功率） | 回归基线 |
| 打转率 | 出现同工具同参数连续重复调用的 case 比例 | 回归基线 |
| pass^k 漂移率 | k 次连跑中轨迹行为不一致的比例（对齐 L6 铁律 1） | 观测项 |

## 现状（2026-07-11）

- 数据 8 条（TRAJ-01~06 原有 + TRAJ-07/08 HITL 续跑难例），全部带 `budget`。
- **runner 待建**：需多轮 thread 锚定——嵌入式 DeerFlowClient 逐轮同 `thread_id` 续发，或走 `_shared/probe_gateway.py` 的 gateway 全栈路径（HITL 中断/续跑必须经 ClarificationMiddleware，`backend/debug.py` 无 checkpointer 测不了，见 CLAUDE.md §3.3）。判定器可复用 `cost_task_score` 的观测抽取思路，逐 turn 比 `assistant_expect`。
