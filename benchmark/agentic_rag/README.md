# Agentic RAG 评测 track（与既有 L1~L7 解耦）

> 独立的**能力 track**，专测「组价 agentic RAG」——需求见根 `AGENTIC_RAG_PRD.md`（FR-1~6）、场景论证见
> `MS.md`「组价为什么需要 Agentic RAG」。**与既有工作解耦**：本目录自持「数据 + 消融编排」，不污染 canonical
> 金标（`L6_agent/norm_faithful`、`L6_agent/cost_task`）；但**判定器（判官）复用 import**——单一尺子，不重造。

## 为什么单开一个 track

agentic RAG 是横跨检索（L3）+ agent（L6）的**能力**，不是某一层。更关键：它的立身价值靠**消融**证明
（agentic vs naive 同集对比），需要一套「数据 + 变体 + 对比脚本」独立演进，塞进按层分的目录会和别的评测缠住。

## 目录

```
agentic_rag/
├── README.md
├── data/
│   ├── norm_agentic.jsonl    # 规范问答（FR-5 诚实拒答 8 + FR-1 拆解 4），AR-N01~12
│   └── cost_agentic.jsonl    # 端到端组价（FR-2/3/6 + 版本超范围），AR-C01~06
├── run_norm_ablation.py      # 规范问答消融 runner（复用 norm_faithful_score 判官）
├── run_cost_agentic.py       # 端到端组价 runner（复用 cost_task_score 判官）
├── compare_ablation.py       # 读各臂结果 JSON → 出 delta 表
└── results/                  # 各臂聚合结果 JSON（compare 的输入）
```

## 解耦边界（一句话）

| 维度 | 归属 | 说明 |
|---|---|---|
| 数据 | **本 track 独立** | `data/*.jsonl`，不进 canonical 金标 |
| 消融编排 | **本 track 独立** | mode/变体标签/结果落盘/delta 对比 |
| 判定器（判官） | **复用既有，import** | `norm_faithful_score` / `cost_task_score` 单一尺子，防两把判官漂移 |
| 检索/agent 本体 | 复用系统 | ce-rag :8100 + vLLM :8099 + lead agent |

## 需求 → 数据 → 指标映射

| FR | 数据 | 判什么 |
|---|---|---|
| FR-5 诚实拒答（招牌） | AR-N01~08 | 漏拒率=0、拒答准确率；禁编（引用回查） |
| FR-1 多跳/拆解 | AR-N09~12 | 答案要点覆盖、上下文召回（拆解题两子主题都要命中） |
| FR-2 综合单价链 | AR-C01~03 | 终态清单码 + GB50854-2013 溯源 + pass^k |
| FR-6 整单闭环 | AR-C04 | 多码终态（⚠️判官现仅校首码，见 note） |
| FR-3 定额换算 | AR-C05 | 清单码 + 换算提示（must_flag） |
| FR-5 版本超范围 | AR-C06 | 拒答不落码、零取数 |

## 怎么跑（服务器；:8100 ce-rag + :8099 vLLM 起齐，宿主机嵌入式跑须 config base_url=localhost:8099）

### 1) 规范问答消融——坐实 agentic delta

两条正交轴，**分开跑才能干净归因**：

- **轴 A · 引用回查**（现成，招牌）：`--mode` 切 `CE_NORM_FAITHFULNESS_CHECK`（关/开 verify_norm）。
  ```
  uv run --project backend python benchmark/agentic_rag/run_norm_ablation.py --mode traditional --variant-label v7 --run-name norm_verifyoff_r1 --no-langfuse
  uv run --project backend python benchmark/agentic_rag/run_norm_ablation.py --mode agentic --variant-label v7 --run-name norm_verifyon_r1 --no-langfuse
  ```
  delta 看**幻觉引用用例率**↓（回查把幻觉引用剥掉/降级）。

- **轴 B · 拆解/多跳**：naive 基线变体 = `benchmark/prompts/lead_agent_v7_naive_norm.yaml`（已建，与 v7 唯一差异是
  `<norm_qa>` 从三步降为「单次检索→直接作答」，红线/路由逐字相同）。切 `config.yaml` 的
  `lead_agent.system_prompt_path` 在它 ↔ `lead_agent_v7.yaml` 之间，`--variant-label` 记当前变体名。
  **两臂都用 `--mode traditional`**（回查都关），delta 才纯归因于「拆解/多跳」。
  ```
  # 1) 把 config lead_agent.system_prompt_path 指向 benchmark/prompts/lead_agent_v7_naive_norm.yaml，再：
  uv run --project backend python benchmark/agentic_rag/run_norm_ablation.py --mode traditional --variant-label naive --run-name norm_naive_r1 --no-langfuse
  # 2) 把 config 指回 benchmark/prompts/lead_agent_v7.yaml，再：
  uv run --project backend python benchmark/agentic_rag/run_norm_ablation.py --mode traditional --variant-label v7 --run-name norm_v7trad_r1 --no-langfuse
  ```
  delta 看**答案要点覆盖**↑、**上下文召回**↑（拆解题两子主题都检索到才不漏点）。**注**：拒答类指标（漏拒率/
  拒答准确率）两臂应基本持平——naive 与 v7 红线相同、都「先检索+零召回拒答」，refuse 行为不是轴 B 的变量，
  作**安全底线**报出即可（要拆诚实拒答的贡献得再单开一轴，改红线而非 RAG 机制）。

- **出 delta 表**（第一个作基线）：
  ```
  python benchmark/agentic_rag/compare_ablation.py benchmark/agentic_rag/results/norm_naive_r1.json benchmark/agentic_rag/results/norm_verifyon_r1.json
  ```

**8B 非确定**：每臂跑 2~3 轮（换 `--run-name`）取稳定值，别拿单轮当结论。

### 2) 端到端组价

```
uv run --project backend python benchmark/agentic_rag/run_cost_agentic.py --run-name cost_ar_v1 --no-langfuse
```

## 诚实边界 / 待办

- **样本小**：norm 可答用例仅 4（要点/召回在其上算）、拒答 8；cost 6 条。n 小波动大，多轮取均值；要更大可答池
  可临时 `--data benchmark/L6_agent/norm_faithful/norm_faithful.jsonl` 借 canonical 跑（判官相同）。
- **naive 提示词变体**：`benchmark/prompts/lead_agent_v7_naive_norm.yaml` **已建**（轴 B 基线，config 默认仍 v7、
  不设为现役）；用法见上「轴 B」。
- **多跳精确条号召回（FR-1 强形态）未做**：gb50016 式 `expected_clauses`+`related_clauses` 召回需真实条号，
  须在服务器对 ce-code chunk 树标注，不在本地编造。**待建** `data/clause_multihop.jsonl` + 召回 runner。
- **整单多码判定器只校首码**：AR-C04 的 `cost_task_score` 对码列表仅可靠比对首个 9 位——多码整单要扩判定器。
- **2024 版口径**：AR-N07 / AR-C06 按现役「仅深圳·2013」判**拒**，与 canonical 遗留 2024-可答用例相反，note 已标，
  待产品裁定谁为准。
