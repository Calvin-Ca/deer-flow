# L2 置信度门控（选码引擎 + 校准）

> 层定义见 `../AGENT_BENCHMARK.md` §2-L2：直配/辅助/转人工的分流质量，**误直配率 ≤ 1% 是最严安全 gate**（高置信直配绕过兜底，错了直接进结果）。§9 B1「选码置信校准 + 8b/32b 决策」归本层。

## 内容

`select_eval/`——self-contained 选码引擎（原 ce-services 退役时迁入），三包：

| 包 | 内容 |
|---|---|
| `common/` | config（服务地址/阈值）、cost_client（:8100 取数）、llm（vLLM 调用） |
| `cost/` | `selection.py` 选码 + 置信阈值（CONFIDENCE_THRESHOLD）；`calibration.py` **置信校准**（B1 主战场） |
| `tools/` | `eval_select.py` 成熟评测入口：Recall@k / 端到端 Top-1 / 候选内 Top-1 / **高置信错码=0 红线** |

## 跑法

- 直接跑引擎评测（服务器，需 :8100 + :8099）：`cd benchmark/L2_gating/select_eval && uv run python -m tools.eval_select`（金标默认读 `../../L3_retrieval/data/match_gold.jsonl`，`--gold` 可换）。
- 挂 Langfuse 的清单匹配评测走 `../L3_retrieval/run_retrieval_experiment.py`（跨层复用本引擎——它一次跑分同时出 L3 召回指标与本层选码/高置信指标，引擎单源、两层各取各的数）。

## 待办（对齐 §9 B1）

用 `eval_select` 暴露的真实 chosen_score 分布（选对 vs 选错）精调 `CE_SELECT_{FLOOR,CEIL,MARGIN}`；τ_high/τ_low 扫参曲线只碰 dev split。
