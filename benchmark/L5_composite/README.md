# L5 复合拆解（占位，待建）

> 层定义与指标见 `../AGENT_BENCHMARK.md` §2-L5：复合请求拆成哪几个子任务、每个子任务落点对不对、结果汇总对不对（子任务集合 P/R/F1 + 子任务级路由准确率 + 汇总正确性）。

## 现状（2026-07-11）

- **专用金标未建**（需按 §4.2 schema 的 `expected_subtasks` 字段造复合样本）。
- 已有零散覆盖，建集时可作种子（勿直接复用为金标，见 §7「不复用既有数据」前提）：
  - `../L1_routing/data/user_requests.jsonl` 含 EH-01 比选/复合的路由用例（只标了顶层落点，没标拆解）；
  - `../L6_agent/trajectory/trajectory.jsonl` TRAJ-03（复合拆解→分派→汇总的多轮运行态）；
  - `../L6_agent/toolcall/toolcall.jsonl` 的复合 MULTI 两 call 用例。
- 前置依赖：FR-X02 比选后端（§9 B2 剩余项）——「哪种更省」需两方案各自组价出真价差，拆解评测才有终态可判。

## 建集顺序

按整体测试梯队，L5 排第三梯队（依赖 L1 单一路由已稳）——见 `../README.md`。
