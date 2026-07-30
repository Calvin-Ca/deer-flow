# PROGRESS

> 由 Claude Code 维护。每完成一个子任务立即更新：状态、产出文件路径、遗留问题。
> 状态取值：`⬜ 未开始` / `🔄 进行中` / `✅ 完成` / `⚠️ 阻塞`

## 当前状态（2026-07-30）

**阶段 1~4 已收口，四组 LoRA adapter 均完成 1500 步训练，可进入阶段 5 评测。**

训练 OOM（O-8）已解决：四组统一采用 `batch 1 × 累积 16`，有效 batch 仍为 16；
20 步冒烟和四组正式训练全部完成，无 OOM / NaN，最终权重及 checkpoint-1500 均已核验。

| 组 | 样本数 | 构成 |
|---|---|---|
| A | 11770 | 条文原文 × 5 模板（无 LLM） |
| B | 9407 | 2352 条文 × 4 视角，不过滤 |
| C | 4727 | B + 可答性/条款准确性/去重 三重过滤（淘汰 49.8%） |
| D | 9187 | C 4727 + 跨条文 2961 + 拒答 1499 |

| 红线 | 状态 |
|---|---|
| 铁律 1 四组超参逐字相同 | ✅ 27 键中仅 4 个数据路径不同 |
| 铁律 2 固定 max_steps=1500 | ✅ 且无 num_train_epochs 并存 |
| 铁律 3 评测集零泄漏 | ✅ 3/386 → 替换后 **0/386** |
| 四组同源（条文库指纹） | ✅ 均为 `87fc3a3f6cd5`，逐组验证 |

评测集 386 题（SC 115 / CC 93 / CA 79 / CV 60 / RF 39），manifest 已与文件对齐。

---

## 阶段 0：环境与仓库初始化

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 0.1 | 目录骨架 + .gitignore（data/ 必须忽略） | ✅ | `src/` `configs/` `results/` `reports/` `data/interim/` `data/processed/` `data/eval/` | .gitignore 追加 norm-ft 规则 |
| 0.2 | requirements.txt + 环境验证 | ✅ | `requirements.txt` | 待服务器 pip install 验证 |
| 0.3 | LLM API 封装（重试、限流、成本统计、失败留痕） | ✅ | `src/utils/llm.py` | DashScope OpenAI-compatible；RPM 令牌桶；tenacity 3 次退避；失败写 data/interim/failed/ |
| 0.4 | wandb 初始化 | ✅ | configs/group_*.yaml `report_to: wandb` | run_name 已绑定各组；wandb.init 在训练启动时由 LLaMA-Factory 调 |

## 阶段 1：条文库构建（2–3 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 1.1 | 规范 PDF 收集与清单确认 | ✅ | `data/raw/` | GB50010/GB50011/GB50007/GB50009/JGJ3，共 5 本，已归 data/raw/ |
| 1.2 | MinerU 解析，产出结构化中间件 | ✅ | `data/interim/parsed/<规范>/hybrid_auto/` | 5 本全解析；HTTP 服务 172.19.2.2:8000，**backend=hybrid-auto-engine 转正**（表格完整率 GB50009 82.8%→100%、GB50007 99%→100%，段落还原更整；抽查数值无幻觉；表格为 HTML `<table>` 含 rowspan，1.4 按此解析）；驱动 `src/parse/run_mineru.py`。产出 md+content_list[_v2]（入 git），middle_json 不入 git（20M/本、可重跑再生），无位图（return_images=false）。**待 1.6 核**：GB50011 hybrid 完整率 80.1%（略低于 pipeline 82.3%），空 body 表抽查 |
| 1.3 | 条款分块（章-节-条层级还原） | ✅ | `data/interim/clauses.jsonl` | **2357 条**（GB50010:513/GB50011:641/GB50007:438/GB50009:200/JGJ3:565），含 520 条附录条款。2026-07-27 修复五处内容丢失后重建（详见阻塞表 B-1~B-5），此前为 1751 条且内容为条文说明而非正文 |
| 1.4 | 表格抽取与绑定 | ✅ | 内嵌于 1.3 产物 | **492 张表，捕获率 100%**（与源 md 的 `<table>` 数逐本比对）；含续表双向 caption 查找；HTML 全部完整（无截断）|
| 1.5 | `refs` 交叉引用抽取 | ✅ | 内嵌于 1.3 产物 | **282 条款有引用，397 个引用命中条文库 393（99%）**。此前因 `ClauseRef.clause_id` 多插连字符（阻塞表 B-7）命中率为 **0%** |
| 1.6 | 条文库质检（连续性/表格完整率/refs 召回） | ✅ | `data/interim/parse_report.md` | 五本全通过。硬门限由 2 条扩到 **6 条**：强制性>0 / 表格HTML完整 / **说明混入=0** / **路径错=0** / **切分渗漏=0** / **表格捕获率=100%**。后四条为 2026-07-27 新增，专拦 B-1~B-5 那类「跑通了但内容全错」的回归 |

## 阶段 2：数据构造（3 天）

> **2026-07-28 全部重建完成**（上游条文库 1653→2357 条修复后）。四组 `clauses_fingerprint` 均为 `87fc3a3f6cd5`，出处逐组验证过（非默认放过）。

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 2.1 | A 组：模板化切分 | ✅ | `group_a/train.jsonl`（**11770 条**） | 5 模板 × 2354 条。补超长条款闸（与 B 同口径，阈值单点导出）跳过 3 条纯查表巨表（E.5 11.5 万字等），11785→11770；这 15 条在 cutoff_len=2048 下只学得到前 5%、砍在表格中间。修 `template_hash`（原 hash 的是 lambda 内存地址，同一代码连跑三次三个值） |
| 2.2 | 合成 prompt 设计 + 小批验证 | ✅ | `configs/prompts/synth_qa.txt` | prompt 已冻结，MD5 入 manifest |
| 2.3 | B 组：全量反向生成 | ✅ | `group_b/train.jsonl`（**9407 条**） | 2352 条文 × 4 视角。失败率 7.4%→**1.3%**（`src/utils/jsonx.py` 修 LaTeX 转义）；`synth_model` 元数据修正（原记 qwen-max，实为 Qwen3-32B-AWQ）；2026-07-29 修复 `--resume` 用本轮新增量覆盖累计 manifest（632→实测 9407）及未定义变量 `total_clauses` |
| 2.4 | 过滤器 1：可答性 | ✅ | `src/filter/answerable.py` | 判官 qwen3-8b（≠ 合成模型），强制 YES/NO、max_tokens=10。淘汰 4203（44.7%），15m44s（原 24h） |
| 2.5 | 过滤器 2：条款准确性 | ✅ | `src/filter/clause_check.py` | 淘汰 216（2.3%）。幻觉条款号 + 数值冲突双检，纯代码不调 LLM |
| 2.6 | 过滤器 3：多样性去重 | ✅ | `src/filter/dedup.py` | 淘汰 261（2.8%）。**修系统性偏向**：`quality_score` 从未实装 → `0.0>=0.0` 恒真 → 永远保留组内靠前者 = 甲方/监理被结构性淘汰。改按固定种子洗牌后贪心，seed 入 manifest |
| 2.7 | C 组产出 | ✅ | `group_c/train.jsonl`（**4727 条**） | 总淘汰 49.8%（原 70.6% 系污染库所致，已落回 15~65% 合理区间）。⚠️ 视角存活率仍不均（设计师 66.5% > 甲方 36.6%）——是判官如实判定的结果，非 bug，须记入 EXPERIMENT.md |
| 2.8 | D1：跨条文样本 | ✅ | `group_d1/train.jsonl`（**2961 条**） | 候选池 refs 390 → **5000 对**（refs 全用 + 同节按 seed 抽样，refs 占比降到 3.9% 同时降低与评测集同源风险）。产出率 29%→**59.2%**，2h00m。补 `--resume`、`--limit` 跨池抽样、jsonx、淘汰原因分类 |
| 2.9 | D2：拒答样本 | ✅ | `group_d2/train.jsonl`（**1499 条**） | 35m31s，失败率 **0.1%**。配额 750:449:300 精确命中。修三处：裸 json.loads→jsonx、四条失败路径全静默→按 §6.6 留痕、有放回抽样（12% 重复）→无放回 |
| 2.10 | D 组合并 | ✅ | `group_d/train.jsonl`（**9187 条**） | c 4727 + d1 2961 + d2 1499。合并时**校验三源出处**：全部同源才盖指纹，否则如实置空（原实现无条件盖当前指纹＝假绿灯）。跨条文占增量 66%（原仅 6%） |

## 阶段 3：评测集建设（2 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 3.1 | 真题来源收集 | ✅ | | 无现成真题，改由模型基于条文库生成 |
| 3.2 | 386 题标注 | ✅ | `data/eval/evalset_v1.jsonl` | 出题模型 **qwen-max**（刻意 ≠ 合成用的 Qwen3-32B-AWQ）。SC 115 / CC 93 / CA 79 / CV 60 / RF 39，约 ¥27 |
| 3.3 | 泄漏检查（铁律 3 红线） | ✅ | `data/eval/leakage_report.md` | **2026-07-28 首次真跑**：3/386 超阈值 → 替换后 **0/386**。四组最高相似度 a 0.839 / b 0.876 / c 0.876 / d 0.893，均在 0.9 下。脚本加：条文库内容指纹校验（防拿旧数据背书）、embedding 改用远程 bge-large（与过滤器③同源）、分组报告 |
| 3.4 | 超标题目替换重出 | ✅ | `data/eval/evalset_v1_replaced.jsonl` | `scripts/replace_leaked_questions.py`。同类型换**未被评测集用过的条文**重出、新题当场查重（>0.9 重试），保留原 id 供阶段 5 对齐。3 题一次通过，cos 0.82~0.84 |
| 3.5 | manifest 与文件对齐 | ✅ | `data/eval/manifest.json` | `scripts/refresh_eval_manifest.py`。原 manifest 记 388 题（实际 386）——「修重复 id」丢 2 题后未重生成，而阶段 5 分题型准确率以配额为分母。现补记泄漏状态 + **比对时的四组指纹**（结论只对特定数据成立） |

## 阶段 4：训练（1.5 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 4.1 | LLaMA-Factory yaml × 4 + DeepSpeed | ✅ | `configs/group_{a,b,c,d}.yaml` `ds_zero2_offload.json` | 权重本地化（ModelScope 预下载）、`template: qwen` |
| 4.2 | 配置一致性校验 | ✅ | `scripts/check_configs.py` | 铁律 1（27 键仅 4 个数据路径可不同）+ 铁律 2（max_steps=1500，禁与 epoch 并存）+ 技术栈约束 + 有效 batch + **四组指纹一致**。缺指纹即报错，不当作通过 |
| 4.3 | F5 调试入口 | ✅ | `scripts/train_debug.py` + `.vscode/launch.json` | in-process 调 `run_exp()` 不 fork torchrun，断点可命中。修两处：①覆盖参数曾被静默丢弃（`--max_steps 20` 会跑成 1500 步）②改为内存合并 dict——llamafactory 见 yaml 后走 OmegaConf.from_cli，要求 `key=value`，透传 argparse 风格会报 `Some keys are not used`。launch.json 不入 git（根 .gitignore 忽略 .vscode/），需手工放到服务器 |
| 4.4 | 冒烟验证（20 步） | ✅ | `checkpoints/_smoke_group_a/` | batch 1 × 累积 16 后 20/20 步通过；99.8s，train loss 1.0648，无 OOM / NaN |
| 4.5 | 显存诊断 | ✅ | `scripts/probe_train_memory.py` | 实测而非估算。`--stage logits` 隔离测 lm_head+CE（不加载 7B 权重，几秒出结果）；`--stage full` 加载真实模型逐阶段打印。hidden/vocab 从模型 config 读，不写死 |
| 4.6 | 四组正式训练 | ✅ | `checkpoints/group_{a,b,c,d}/` | 四组均完成 1500 步；最终 adapter + checkpoint-1500 全部核验。A/B/C/D 分别 1:52:03 / 1:41:59 / 1:40:03 / 1:41:11，总计 6:55:16 |

训练实测（有效 batch=16，每组均处理约 24000 样本次；loss 受数据难度影响，不可横向当作模型优劣）：

| 组 | 数据量 | 有效 epoch | train loss | runtime | samples/s |
|---|---:|---:|---:|---:|---:|
| A | 11770 | 2.0381 | 0.3770 | 6723s | 3.570 |
| B | 9407 | 2.5511 | 0.9687 | 6119s | 3.922 |
| C | 4727 | 5.0677 | 0.6643 | 6003s | 3.998 |
| D | 9187 | 2.6096 | 0.8968 | 6071s | 3.953 |

## 阶段 5：评测（1 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 5.1 | vLLM 多 adapter 批量推理（6 个模型 × 386 = 2316） | ✅ | `scripts/build_eval_fewshot.py`、`configs/prompts/eval_fewshot_rejections.json`、`src/eval/run_inference.py`、`scripts/eval_inference.sh`、`scripts/serve_eval.sh`、`tests/test_{build_eval_fewshot,eval_inference}.py`、`results/{run_id}/raw/` | 正式推理已完成 6×386；各模型无空答案，原始 JSONL 只读保留。截断数：base 10、base_fewshot 14、group_a 46、group_b 0、group_c 0、group_d 4，已由自动评分生成健康度指标 |
| 5.2 | 条款号抽取 + 归一化 | ✅ | `src/eval/clause.py` | 支持带标准号/中文简称/纯条款号三种形式；含 clause_f1 和 is_hallucinated |
| 5.3 | 条款引用 F1 + 硬幻觉率 | ✅ | `src/eval/scoring.py`、`scripts/score_eval.py`、`results/{run_id}/metrics/` | 同时输出逐题指标、宏平均与微平均 F1，以及无效条文引用率 |
| 5.4 | 数值精确匹配判分 | ✅ | `src/eval/scoring.py`、`scripts/score_eval.py` | Decimal 精确匹配；分别报告完整命中率和数值项命中率，无金标题不进入分母 |
| 5.5 | LLM judge（盲评+乱序） | ⬜ | | judge 模型 ≠ 合成模型 |
| 5.6 | 拒答二分类判定 | ✅ | `src/eval/scoring.py`、`scripts/score_eval.py` | 启发式拒答分类；成对报告拒答准确率、应拒答召回率、非拒答误拒率 |
| 5.7 | 指标汇总，填 EXPERIMENT.md §3 | 🔄 | `results/{run_id}/summary.csv`、`results/{run_id}/scoring_manifest.json` | 自动汇总入口已完成；待在服务器上对正式 run 运行并将结果填入实验记录 |

## 阶段 6：验证与报告（1.5 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 6.1 | 人工抽检 100 条 + Kappa | ⬜ | | 我来标，你出抽样和计算脚本 |
| 6.2 | 结果可视化 | ⬜ | `results/{run_id}/figs/` | |
| 6.3 | 实验报告 | ⬜ | `reports/report.md` | 按 EXPERIMENT.md §4 组织 |
| 6.4 | 一键复现脚本 + README | ⬜ | | |
| 6.5 | 评测集脱敏开源 | ⬜ | | 只开评测集，不开条文库 |

---

## 待我确认的决策点（不要自行拍板）

- [x] 土木细分方向 → **结构方向**（GB50010/GB50011/GB50007/GB50009/JGJ3，2026-07-24 确认）
- [x] 合成 prompt 措辞 → **已冻结**（2026-07-25，答案不加长度限制）
- [x] 合成模型 → **本地 Qwen3-32B-AWQ**（172.19.2.2:8001，零费用）
- [ ] 多样性去重阈值（阶段 2.6，默认 0.85，待确认是否调整）
- [x] 评测集 400 题的最终类别配额 → **SC×120/CC×100/CA×80/CV×60/RF×40**（2026-07-25 落盘）
- [ ] 是否做 rank 消融扩展

## 阻塞与遗留问题

> 2026-07-28：条文库修复引发的停线级问题**已全部解决**，四组重建完毕、铁律 1/2/3 全过。
> 下面只留仍然开放的项；已解决项的教训沉淀在文末「贯穿性教训」。

### ⚠️ 开放项（影响评测实现或报告解读）

| # | 问题 | 影响 | 处置 |
|---|---|---|---|
| O-1 | **C 组条文覆盖缺口**：过滤后 397 条条文（16.9%）一条样本不剩，导致评测集 **36 题（10.4%）零覆盖** | C/D 组对这些题结构性答不出。若不标注，会被读成「过滤有害」，而本项目命题恰是「高质量 < 数量」——归因方向会反 | **不补数据**（补了 C 就不再是「B + 过滤」，破坏消融定义）。改为阶段 5 出分时按「金标条文是否被该组覆盖」分列两个口径：原始分 + 覆盖调整分 |
| O-2 | **C 组视角存活率不均**：设计师 66.5% > 施工员 51.9% > 监理 45.9% > 甲方 36.6% | C/D 的视角分布相对 B 偏移 | **不修**。判官没判错——B 组提示词让甲方问「对工程安全性的影响」，本就超出单条条文；被淘汰的正是「答不了却硬答」的样本。记入 EXPERIMENT.md 作为过滤策略的固有代价 |
| O-3 | 过滤器③无质量信号 | 去重取舍现为随机（已消除视角偏向），但留下的那条不保证优于被淘汰的 | 已在 docstring 写明诚实边界。要真按质量取舍需另加一轮 LLM 打分，本项目不做 |
| O-4 | 评测集金标覆盖 61% | 判断题/推导题本无数值金标，不进 5.4 分母 | 靠 5.3 条款引用 F1 与 5.5 LLM judge 评分；分母已记入 manifest |
| O-5 | 出题模型与合成模型同厂不同代（qwen-max vs Qwen3-32B-AWQ） | 独立性弱于跨厂 | 泄漏检查实测 0/386，风险已量化。写入 EXPERIMENT.md 已知局限 |
| O-6 | 服务器上存在两套 Python（conda 3.13 / 项目 venv 3.12） | 数据阶段无碍（纯 CPU），**训练阶段 torch+CUDA 版本不符会直接失败** | 开训前确认走 `ce-code/.venv`（CLAUDE.md §2 定 3.12 + uv） |
| O-7 | `--workers` 提升有限 | 32B 单流 3.9 tok/s 是瓶颈，非并发问题 | 已探明，不再投入 |
| O-9 | CLAUDE.md §2 与实现不符 | 宪法写「DeepSpeed ZeRO-2 **with CPU offload**」，而 `ds_zero2_offload.json` 已于 07-27 移除 offload（服务器无 nvcc，`cpu_adam` 编译不了）。日后照宪法复现会以为 offload 一直开着 | 待修正宪法措辞，或补一句环境限制说明 |

#### ✅ 已解决 O-8：训练 OOM

**现象**：`train_debug.py` 跑 A 组 20 步冒烟，加载模型后前向即 OOM——
`Tried to allocate 1.99 GiB. GPU has 23.65 GiB total, 1.26 GiB free, this process using 22.38 GiB`。
GPU 2 经 `nvidia-smi` 确认空闲（3MiB），非其他进程占用；GPU 1/3 被 vLLM 服务占着不能动。

**实测归因**（`scripts/probe_train_memory.py`，非估算）：

| batch | lm_head+交叉熵峰值 | 扣 lm_head 权重后净占 |
|---|---|---|
| 2 | 9.17 GB | 8.13 GB |
| 1 | 5.10 GB | 4.07 GB |

机制：Qwen2.5 词表 152064，是 hidden(3584) 的 **42 倍宽**。logits 张量
`[2, 2048, 152064]` = 6.23 亿元素；算交叉熵要转 fp32、存 log_softmax、反向存梯度，
同时活着好几份 → 峰值 9.17 GB。作为对比单层 hidden state 只有 0.027 GB，**相差 335 倍**。
`gradient_checkpointing` 管不着它——lm_head 与 loss 在 transformer 层之后，不在重算范围内。

显存账：权重 15.20 + logits/CE 8.13 = **23.33 GB**，已占满 23.65 GB 的卡，
还没算 LoRA 状态(1.3)、激活(0.8)、DeepSpeed buffer(~1.6)。

**已采纳改法（2026-07-29）**：`per_device_train_batch_size 2→1` + `gradient_accumulation_steps 8→16`。
有效 batch 仍为 16，**梯度数学等价**（已用 16 条样本的四种拆法验证：累积梯度逐位相同，
只有显存峰值不同）；`max_steps=1500` 的语义不变（步=参数更新次数），铁律 2 不受影响。
实测省 4.06 GB，反推余量 3.3 GB。代价：每优化步多一次前向反向，约慢 10~20%。

**关闭依据（2026-07-30）**：A 组 20 步冒烟通过，随后 A/B/C/D 四组均完成 1500 步；
GPU 0 实测训练显存 21394/24564 MiB（余量约 3.1 GiB），未再出现 OOM。

**未采纳的两个方案**：
- 去掉 DeepSpeed（省 ~1.6GB）：单卡切不了、offload 已移除，ZeRO-2 在此确为纯开销；
  但 batch 1 已够，且 CLAUDE.md §2 把它列为技术栈约束，为可有可无的 1.6GB 改宪法不划算
- liger kernel 的 fused CE（logits 那 8GB 可压到 <1GB，且更快）：技术上最优，
  但需装新依赖、换 loss 实现、验证数值。**先跑通再优化**

该变更已同步四组并记入 `EXPERIMENT.md`；四组正式训练均从新配置起跑，未混用旧配置。

### 待确认的决策点

- [ ] 多样性去重阈值 0.85 是否调整（当前淘汰 2.8%，偏松）
- [ ] 是否做 rank 消融扩展

---

## 一条贯穿性教训

**这个项目里最危险的失败，全都不报错。**

程序正常退出、指标落在合理区间、报告格式工整——只有内容是错的。逐一列举：

| 现象 | 真相 |
|---|---|
| 条文库 1653 条，质检全过 | 抓的是「条文说明」不是正文，四组数据全被污染 |
| 判官报告「50/50 解析失败」但结论正常 | API key 缺失被 `except` 吞掉，一次模型调用都没发生 |
| 诊断报「49% 输出被截断，需调高 max_tokens」 | 日志只存 `raw[:1000]`，量的是日志形状不是输出形状；真实 max_tokens 用了 590/3000 |
| B 组 manifest 写 `synth_model: qwen-max` | 实际是 Qwen3-32B-AWQ，字面量写了三处、换模型时漏改一处 |
| `template_hash` 每次都在变 | hash 的是 lambda 的内存地址，既测不出改动又天天变 |
| 「多样性去重」淘汰率正常 | `quality_score` 从未实装，`0.0>=0.0` 恒真 → 实为「永远保留施工员视角」 |
| D 组 manifest 带着绿色指纹 | 合并时无条件盖当前指纹，从不检查三个来源的出处 |
| `check_configs` 打「✅ 全部通过」 | 三组缺指纹时 `set` 长度为 1，把「没验证」当成了「验证通过」 |
| 评测集 manifest 记 388 题 | 实际 386，而阶段 5 的分题型准确率以它为分母 |

**共同结构**：某个环节把「未知」静默转成了「正常」——异常被吞、缺失被跳过、
默认值恰好合法。它们不会在运行时暴露，只会在最终结论里以「实验结果不符合预期」
的形式出现，而那时已无从归因。

**应对方式**（本项目已固化的做法）：
1. **失败必须留痕**（§6.6）——且存完整内容，不截断（截断会让事后诊断量错对象）
2. **红线检查宁可报「无法验证」，不给虚假绿灯**——缺指纹即失败，而非跳过
3. **每个断言都要能被反向验证**——注入一个已知错误，看检查抓不抓得住
4. **同一事实只写一处**——模型名、阈值、门槛写多份必然漂移，已因此吃亏三次
5. **诊断工具本身也会说谎**——它测的可能是日志/元数据的形状，而非真实对象
