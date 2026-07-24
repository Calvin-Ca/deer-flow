# PROGRESS

> 由 Claude Code 维护。每完成一个子任务立即更新：状态、产出文件路径、遗留问题。
> 状态取值：`⬜ 未开始` / `🔄 进行中` / `✅ 完成` / `⚠️ 阻塞`

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
| 1.3 | 条款分块（章-节-条层级还原） | ✅ | `data/interim/clauses.jsonl` | 1751 条（GB50010:401/GB50011:439/GB50007:308/GB50009:100/JGJ3:503）；三段式条款号过滤两段式节号；去重2015修订版双公告 |
| 1.4 | 表格抽取与绑定 | ✅ | 内嵌于 1.3 产物 | 249 张表（含续表双向 caption 查找）；HTML 全部完整（无截断）|
| 1.5 | `refs` 交叉引用抽取 | ✅ | 内嵌于 1.3 产物 | 214 条款有引用；复用 src/eval/clause.py 归一化 |
| 1.6 | 条文库质检（连续性/表格完整率/refs 召回） | ✅ | `data/interim/parse_report.md` | 五本全通过；门限：强制性>0 + 表格HTML完整；caption率参考（续表无caption属正常）|

## 阶段 2：数据构造（3 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 2.1 | A 组：模板化切分 | ⬜ | `data/processed/group_a/` | |
| 2.2 | 合成 prompt 设计 + 50 条小批验证 | ⬜ | `configs/prompts/synth_qa.txt` | **需我确认后冻结** |
| 2.3 | B 组：全量反向生成 | ⬜ | `data/processed/group_b/` | 先报预估费用 |
| 2.4 | 过滤器 1：可答性 | ⬜ | `src/filter/answerable.py` | |
| 2.5 | 过滤器 2：条款准确性（核心） | ⬜ | `src/filter/clause_check.py` | 需条款号归一化 |
| 2.6 | 过滤器 3：多样性去重 | ⬜ | `src/filter/dedup.py` | 阈值需我确认 |
| 2.7 | C 组产出 + 淘汰率统计 | ⬜ | `data/processed/group_c/` | 淘汰率异常需停下 |
| 2.8 | D1：跨条文样本（基于 refs） | ⬜ | | 含"单条可答则淘汰"校验 |
| 2.9 | D2：拒答样本 | ⬜ | | 三类配额 5:3:2 |
| 2.10 | D 组合并 | ⬜ | `data/processed/group_d/` | |

## 阶段 3：评测集建设（2 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 3.1 | 真题来源收集 | ⬜ | | 注考真题优先 |
| 3.2 | 400 题标注（含 gold_clauses / gold_values） | ⬜ | `data/eval/evalset_v1.jsonl` | 人工为主，我参与 |
| 3.3 | 泄漏检查（vs 四组全部训练数据） | ⬜ | `data/eval/leakage_report.md` | **红线，见 CLAUDE.md 铁律 3** |
| 3.4 | 超标题目替换重出 | ⬜ | | 替换不是删除 |

## 阶段 4：训练（1.5 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 4.1 | LLaMA-Factory yaml × 4 + DeepSpeed 配置 | ✅ | `configs/group_{a,b,c,d}.yaml` `configs/ds_zero2_offload.json` | 数据路径待 PDF 解析完后填入 |
| 4.2 | 配置一致性自动校验脚本 | ✅ | `src/utils/check_configs.py` | `python -m src.utils.check_configs` |
| 4.3 | 四组训练 | ⬜ | `checkpoints/` | 记录时长/显存/loss 曲线 |

## 阶段 5：评测（1 天）

| # | 任务 | 状态 | 产出 | 备注 |
|---|---|---|---|---|
| 5.1 | vLLM 多 adapter 批量推理（6 个模型 × 400） | ⬜ | `results/{run_id}/raw/` | 参数见铁律 4 |
| 5.2 | 条款号抽取 + 归一化 | ✅ | `src/eval/clause.py` | 支持带标准号/中文简称/纯条款号三种形式；含 clause_f1 和 is_hallucinated |
| 5.3 | 条款引用 F1 + 硬幻觉率 | ⬜ | | 全自动 |
| 5.4 | 数值精确匹配判分 | ⬜ | | |
| 5.5 | LLM judge（盲评+乱序） | ⬜ | | judge 模型 ≠ 合成模型 |
| 5.6 | 拒答二分类判定 | ⬜ | | 成对报告 |
| 5.7 | 指标汇总，填 EXPERIMENT.md §3 | ⬜ | `results/{run_id}/summary.csv` | |

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
- [ ] 合成 prompt 措辞（阶段 2.2）
- [ ] 质量分阈值 / 多样性去重阈值（阶段 2.6）
- [ ] 评测集 400 题的最终类别配额
- [ ] 是否做 rank 消融扩展

## 阻塞与遗留问题

| 日期 | 问题 | 影响 | 状态 |
|---|---|---|---|
| | | | |
