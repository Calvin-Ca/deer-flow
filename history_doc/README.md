# 历史设计文档（archive）

本目录归档 CE 造价 agent 早期的**设计 / 规划 / 复盘 / 架构**记录。它们反映的是当时的构想与决策过程，
**不代表当前代码架构**——现状以根 `CLAUDE.md`、`backend/`、`ce-code/` 及 `benchmark/` 的活文档为准。
保留于此供追溯"为什么这么做"，不作为开发时的权威依据。

| 文件 | 内容 |
|---|---|
| `AGENT_MS.md` | 项目业务简介 / 造价全流程背景 |
| `AGENT_PRD.md` | 早期两 Agent（智能组价 / 规范问答）需求与路由决策表（benchmark 路由金标仍溯源至此 §4.3） |
| `AGENT_PROBLEM.md` | 工程化落地问题复盘（红线、弱模型可靠性等） |
| `AGENT_DEERFLOW-NOTE.md` | DeerFlow harness 学习笔记 |
| `architecture-doc.html` / `architecture-target.html` | 架构现状 / 目标态设计图 |
| `cost-agent-architecture.html` | 组价 agent 架构设计图 |

> 注：这些文档里出现的 `ce-services`（:8101 任务层）、前端组价 widget、13 节点 HITL 图等**均已退役**，
> 组价编排现由 deer-flow 内嵌 `cost_workflow_*` + `ce-rag`/`ce-db` MCP 承载。
