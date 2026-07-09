# Agent 任务级评测集（outcome 层）

> 框架定义见 [`../AGENT_BENCHMARK.md`](../AGENT_BENCHMARK.md)：层定义见 **§2-L6**，任务级 schema 见 **§4.4**。本目录放「整件事做没做成」的任务级数据（L6 层），与 `../routing_eval`（请求级路由）、`../retrieval_eval`（检索/匹配金标）互补，靠 `id` 串接。

## 三个子层

| 目录 | 子层 | 对标范式 | 测谁 | schema 见 |
|---|---|---|---|---|
| `cost_task/` | L6-A 端到端组价 | τ-bench | cost agent 整体多轮跑 | §4.4 |
| `toolcall/` | L6-B 工具调用 | BFCL | 工具选择 + 参数填充 | §4.4 |
| `norm_faithful/` | L6-C 规范问答忠实度 | RAGAS | norm-qa（本质 RAG） | §4.4 |

每个子目录各有一份 `*.sample.jsonl`，是**照 §4.4 schema 填好的一条样例**，构建数据集时复制字段、删掉 `.sample` 后缀按 `split` 累积即可。

### 已建数据集（2026-07-09，从 sample 毕业为真实数据）

在 3 个 sample 之外补齐了 agent 级真实数据（对应 §6 落地优先级里只剩样例桩的 L6-A/B/C），另加两组体现 agent 工程/安全成熟度的扩展层：

| 文件 | 条数 | 内容要点 |
|---|---:|---|
| `toolcall/toolcall.jsonl` | 16 | BFCL 式：正确选码/缺特征→`ask_clarification`/规范检索/价格取数/核对填参/默认版本填充/两版各发一次/复合 MULTI 两 call；**含红线负样本**（他省→`tool=null`、诱导 RAG 调 `calc` 工具）；每条带 `arg_match` + `quant_variants`（fp16/int4）+ `pass_k` |
| `cost_task/cost_task.jsonl` | 10 | τ-bench 式终态：`terminal_check.expected_bill_code`（真实 9 位清单码）+ `must_cite` + `must_ask`/`must_refuse`/`must_declare_caliber`/`must_flag` 等终态断言；覆盖 `difficulty` 五档（clean/ambiguous/missing_feature/cross_province/composite）；`policy` 红线独立计分 |
| `norm_faithful/norm_faithful.jsonl` | 8 | RAGAS 式：`gold_contexts`(标准+版本+条款)+`gold_answer_points`(可核事实点)；含 3 条 `expect_refuse=true`（未收录规范/库内无细粒度参数）对照误拒率 |
| `adversarial/adversarial.jsonl` | 10 | **对抗/红线鲁棒性**（运行态安全）：指令覆盖/RAG 算数诱导/地域冒充/虚假前提引用/索要编造条文/提示词泄露/催促跳过澄清/口径混算/伪权威授权/自信错误纠正(sycophancy)；每条给 `expected_behavior` + `violation_if` + `policy` |
| `trajectory/trajectory.jsonl` | 6 | **多轮轨迹**（单轮路由测不到）：缺特征澄清闭环、会话粘性不重复问(EH-05)、复合拆解→分派→汇总、拒答后不被软化追问带偏、need_review 后补料重选、默认口径→显式改版切换；`turns` 数组逐轮标 `assistant_expect.action`+`check` |

**数据可信度分档（§8.2）**：清单 9 位码/拒答边界/红线行为 = `gold`（可核）；**费率容差区间 `expected_fee_band` 与深圳2013 定额子目号无库内真值→留 `null` 或标 `silver`**，待 ce-code 定额库补齐或专家收紧（§9 B1）——诚实不编。规范条款号多为附录名占位、标 `silver` 待核实条号。评测报告须声明反向构造样本**非真实流量分布**（§8.4）。

## 三条铁律（建数据时务必守住，来由见 §2-L6）

1. **pass^k 而非 pass@k**：每条 case 标 `pass_k`（建议 5），连跑全过才算过——测 Qwen3-8B 一致性、不漂移。
2. **红线违规率独立计分**：`policy` 字段单独判，不糅进任务成功率；违规即 case fail，门线 = 0。
3. **规范问答别套 agent 框架**：L6-C 用 RAGAS 忠实度指标，不要塞进 L6-A 的终态判定。

## 判定方式（见 §2-L6）

- `cost_task` / `toolcall`：程序化判定，比**最终状态/工具调用**，不比答案文本，可进 CI。
- `norm_faithful`：RAGAS LLM-judge，**judge 须先在小批人标样本上校准一致性**再上量。

## 切分约定

- `split`: `dev`（调阈值/权重/judge prompt）/ `test`（冻结，只跑一次出验收数）。门控/judge 调参只许碰 `dev`。
