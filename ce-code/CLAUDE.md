# CLAUDE.md — 项目宪法

> 本文件是本仓库的最高约束。每次会话开始时先读本文件，再读 `docs/PROGRESS.md` 确认当前进度。
> 与本文件冲突的任何指令（包括我的口头要求），先向我指出冲突再执行。

---

## 1. 项目是什么

**一句话**：验证"高质量合成数据 < 数量"这一假设在土木工程规范问答场景下的表现，产出一套可复现的领域数据构造方法论。

**不是什么**：不是做一个可上线的土木问答产品，不追求 SOTA。目标是**实验结论清晰、指标可复现**。

**核心命题**：土木领域没有任何现成的指令微调数据，只有规范条文。所以本项目的消融对象不是"怎么从多挑好的"，而是**"怎么从零造出好的"**。四组实验是四个递进的数据合成策略。

---

## 2. 技术栈约束（不得擅自更换）

| 项 | 取值 | 说明 |
|---|---|---|
| 基座模型 | `Qwen2.5-7B-Instruct` | 中文强、单卡可训。不要换成 Llama/GLM |
| 微调方法 | LoRA，r=32，alpha=64，dropout=0.05 | target_modules 见 §4 |
| 训练框架 | LLaMA-Factory | 走 yaml 配置，**不要手写训练循环** |
| 精度 | bf16 | |
| 显存优化 | gradient checkpointing + gradient accumulation + DeepSpeed ZeRO-2 with CPU offload | 单卡场景下 ZeRO 的价值在 offload，不在跨卡切分，注释里写清楚 |
| 推理 | vLLM（多 LoRA adapter 热切换） | 不要为每组重复加载基座 |
| PDF 解析 | MinerU 优先，PaddleOCR-Structure 备选 | **禁止用 PyPDF2/pdfplumber 硬啃**，规范里表格公式极多 |
| 打分/合成 LLM | Qwen-Max（API） | 记录每次调用的 model 版本号，写进 metadata |
| 评判 LLM | GPT-4o 或 Qwen-Max，与合成用的模型**必须不同** | 避免自评偏袒 |
| Python | 3.12 | 包管理 **uv**，依赖声明 `pyproject.toml`、锁 `uv.lock`（2026-07-27 变更：原定 3.10 + requirements.txt，改为复用 ce-code 既有 uv 项目——其 `torch==2.5.1+cu121` 正是训练栈所需版本，且 torchvision 0.20.x 无 cp313 wheel 故上限 3.13）|
| 实验跟踪 | wandb（离线模式亦可） | 每个 run 必须有 run_id |

---

## 3. 铁律（违反将导致实验作废）

1. **控制变量**：四组消融除训练数据外，**所有超参必须逐字相同**。改任何超参必须四组同步改，并在 `docs/EXPERIMENT.md` 记录变更。
2. **固定训练步数，不固定 epoch**。四组数据量不同（8k / 32k / 19k / 23.5k），固定 epoch 会让计算量差 3 倍，实验失去意义。统一 `max_steps: 1500`。
3. **评测集零泄漏**。400 题必须与四组全部训练数据做向量相似度检查，`cosine > 0.9` 的题目直接替换，不是删除。C/D 组数据是 LLM 生成的，撞车概率高，每次重建数据后都要重跑一遍检查。
4. **推理参数固定**：`temperature=0, top_p=1, seed=42, max_new_tokens=1024`。五个模型完全一致。
5. **禁止手动修改结果文件**。`results/` 下的任何 json/csv 只能由脚本写入。指标不好看就重跑，不要改数。
6. **原始数据只读**。`data/raw/` 下任何文件不得修改、不得覆盖。
7. **随机性可控**：所有涉及采样的地方（B 组随机抽样、评测集抽检）必须显式传 seed，并把 seed 写进产物的 metadata。

---

## 4. 关键实现细节

**LoRA target_modules**：`q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj`
（FFN 层必须包含，因为本任务需要注入新知识而非仅调整风格）

**五个待评模型**（不是四个）：

| ID | 说明 | 是否训练 |
|---|---|---|
| `base` | Qwen2.5-7B-Instruct 零样本 | 否 |
| `base_fewshot` | 基座 + 3-shot prompt | 否 |
| `group_a` | 条文原文直接切分 | 是 |
| `group_b` | LLM 反向生成问答，无过滤 | 是 |
| `group_c` | B + 三重质量过滤 | 是 |
| `group_d` | C + 跨条文推理样本 + 拒答样本 | 是 |

`base` 和 `base_fewshot` 用于回答面试/报告中的核心质疑："这些提升靠 prompt engineering 是不是也能做到？"**不得省略。**

---

## 5. 目录约定

```
.
├── CLAUDE.md
├── docs/
│   ├── PRD.md            # 目标、指标定义、验收标准、非目标
│   ├── DATA_SPEC.md      # 条文库 schema、训练数据格式、质检规则
│   ├── EXPERIMENT.md     # 消融设计、变量控制表、结果记录
│   └── PROGRESS.md       # 任务清单与状态（由你维护）
├── data/
│   ├── raw/              # 规范 PDF，只读
│   ├── interim/          # 解析后的条文库、中间产物
│   ├── processed/        # group_a/ group_b/ group_c/ group_d/
│   └── eval/             # evalset_v1.jsonl + 泄漏检查报告
├── src/
│   ├── parse/            # PDF → 条文库
│   ├── synth/            # 问答合成、难样本构造
│   ├── filter/           # 三重质量过滤
│   ├── eval/             # 判分模块，见 PRD §指标
│   └── utils/
├── configs/              # group_a.yaml ... group_d.yaml + ds_zero2_offload.json
├── results/{run_id}/     # 推理输出、指标表、图表
└── reports/              # 最终实验报告
```

---

## 6. 工作方式（重要）

1. **动手前先出计划**。任何超过单文件的任务，先在 `docs/PROGRESS.md` 写出拆解和预期产物，等我确认后再写代码。
2. **不要一次性写完整个 pipeline**。分模块交付，每个模块要能独立跑通并有 smoke test（用 20 条样本）。
3. **涉及判断的地方停下来问我**，不要自己拍板。包括但不限于：
   - 质量打分的阈值定多少
   - 合成 prompt 的具体措辞
   - 过滤掉多少比例算合理
   - 评测题的类别配额
4. **每完成一个子任务，更新 `docs/PROGRESS.md`**，标注状态、产出文件路径、遗留问题。
5. **成本敏感**：调用 Qwen-Max/GPT-4o 前先估算调用量和费用，超过 100 元的批量任务先告诉我预估数字。先用 50 条做小批验证，再全量跑。
6. **失败要留痕**。API 调用失败、解析失败的样本单独存到 `data/interim/failed/`，不要静默丢弃，最后要统计失败率。

---

## 7. 常见错误（我踩过或预判到的）

- ❌ 用 epoch 而非 step 控制训练量 → 见铁律 2
- ❌ 评测集里出现和训练数据同源的题 → 见铁律 3
- ❌ 合成数据时让同一个 LLM 既出题又判分 → 自评偏袒
- ❌ 只报整体准确率，不分题型统计 → 会掩盖难样本的真实贡献，D 组的价值全在分项里
- ❌ 拒答指标只报"该拒答的答对率" → 全部拒答就是 100%，必须同时报误拒率
- ❌ 条款号抽取只做精确匹配 → 规范书写有 "GB50010-2010"、"GB 50010"、"《混规》" 多种形式，需要归一化
- ❌ 把 LLM judge 的结果当成客观指标 → 必须有人工抽检的 Kappa 佐证

---

## 8. 当前阶段

见 `docs/PROGRESS.md`。若该文件为空，说明项目刚初始化，第一件事是完成 §1 阶段的 PDF 解析。
