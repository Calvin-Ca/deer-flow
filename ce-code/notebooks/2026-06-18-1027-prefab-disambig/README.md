# 实验 2026-06-18-1027 · 现浇/预制消歧（cast_type 标记 + 召回 down-rank）

> 状态：🟡 进行中（待服务器跑）。承接：[[experiments.md E6]]（真实结算 gold 评测暴露现浇/预制不可分）。

## 1. 背景与假设

- **问题**：E6 的 2013 真实 gold 评测中，柱/板本体大面积 miss——`010502001 矩形柱`（现浇）被
  `010509001 矩形柱`（预制）抢，`010505001 有梁板`（现浇）被 `010512002 空心板`（预制）抢。
  根因：现浇/预制**同名**，索引 chapter 相同（都「附录 E 混凝土…」），caption（"预制混凝土柱"）建库时被丢，
  query 又无"现浇/预制"信号 → dense 无从区分。
- **假设**：建库期从 caption/unit 派生 `cast_type`（现浇/预制）标记入索引，召回期对「query 未提预制却命中预制项」
  down-rank，能把这 ~11 条本体翻正，且对其余样本零回归。

## 2. 配置

- **代码版本**：commit `75bf2650`
  - `cost/bill_index.py`：`cast_type(caption, unit)` 派生标记 + Milvus 新增 `cast_type` 字段（PG/jsonl 两源覆盖）
  - `cost/bill_match.py`：`_prefab_penalty` 并入 `_structural_reorder`（query 无预制 → 下压预制候选）
- **本次只动的变量**：新增 cast_type 标记 + 预制 down-rank（嵌入文本不变、专业过滤不变、structural 其余规则不变）。
- **数据**：gold `data/eval_set/match_gold_2013.jsonl`（82 条）/ collection `cost_bill_spec_kb_2013`
  （**须重建**——cast_type 是新字段）/ 源 `data/structured/GB-50500-2013/bill_spec.jsonl`（绕 PG 直读）
- **服务依赖**：Milvus :19530 + embedding bge-large :8097
- **关键参数**：top_k=10 / `--code-prefix 01,03` / structural=on / rerank=off

## 3. 运行脚本

服务器（ce-code 根）：

```bash
git pull
bash notebooks/2026-06-18-1027-prefab-disambig/run.sh 2>&1 | tee notebooks/2026-06-18-1027-prefab-disambig/results/run.log
```

`run.sh`：① 重建 2013 collection（带 cast_type）→ ② `--code-prefix 01,03` 重测。

## 4. 结果

> 待服务器跑后贴 `results/run.log` 的汇总行 + 关键逐条（柱/板那几条）。

| 指标 | E6 基线（专业过滤，cast_type 前） | 本次（+cast_type down-rank） | 变化 |
|---|---|---|---|
| Top-1 | 40% | | |
| Top-3 | 43% | | |
| Recall@10 | 51% | | |
| MRR | 0.427 | | |
| 平均命中秩 | 2.05 | | |

关键逐条（预期翻正）：
- `柱；混凝土强度C40` → 期望 `010502001 矩形柱`（现浇），不再是 `010509001`（预制）
- `板；混凝土强度C35` → 期望 `010505001 有梁板`（现浇），不再是 `010512xxx`（预制空心/大型板）

## 5. 分析与结论

- **对照**：（柱/板本体翻正几条？有无回归？）
- **分桶**：措施项（0117）应**仍全灭**（标准错配，本次未动）；本体桶看真实提升。
- **结论**：（✅/⛔/🟡 + 一句话）
- **下一步**：措施项标准错配收口（换 GB 50854-2013 建库 / 或 gold 剔除 0117 只评本体桶）。

> 跑完在 `experiments.md` 顶部加 E7 精炼结论并链回本文件夹。
