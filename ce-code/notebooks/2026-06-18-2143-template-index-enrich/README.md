# 实验 2026-06-18-2143 · 模板索引文本增强（caption 子表类别注入嵌入）

> 状态：🟡 进行中（待服务器跑）。承接：[[experiments.md E9]]（召回诊断：40/40 miss 在库，一半是措施码）。

## 1. 背景与假设

- **问题**（E9 召回诊断）：2013 gold Recall@10=56%，40 条 miss 全在库；**约一半是措施码（0117）**：
  `011702011 砼墙模板`×6、`011702024 楼梯模板`×4、`011702006 梁模板`×2…。根因——措施码的 **name 是结构名**
  （"矩形梁"/"有梁板"），**不含"模板"**；嵌入文本 = name+特征+chapter，chapter 只到泛泛"附录S措施项目"，
  真正的子类（"混凝土模板及支架"）在 **caption** 里、没进嵌入 → 查询"梁模板支撑架"召不回。
- **假设**：从 caption 派生「子表类别」注入嵌入文本（措施码补回"模板/脚手架/超高"等信号），措施桶召回抬升；
  对本体项零伤害（本体 caption="现浇混凝土柱"，补现浇/分部上下文，与 cast_type 互补）。

## 2. 配置

- **代码版本**：commit `<本实验 commit>`
  - `cost/bill_index.py`：新增 `caption_category(caption)`（剥表号前缀 + (编码)后缀，留子类）；
    `bill_embed_text` 加 `category` 参数（与 name 相同则不重复）；`_fetch_bills`/`_read_bills_jsonl` 派生
    `category`、`build` 注入嵌入文本。
- **本次只动的变量**：嵌入文本增加 caption 子表类别（cast_type/structural/专业过滤/模型均不变）。
- **数据**：collection `cost_bill_spec_kb_2013`（**重建**，嵌入文本变了）/ gold `match_gold_2013.jsonl`（91 条，不变）
- **依赖**：Milvus + embedding bge-large :8097 + PG（--spec 2013 读 PG）

## 3. 运行脚本

```bash
git pull
bash notebooks/2026-06-18-2143-template-index-enrich/run.sh 2>&1 | tee notebooks/2026-06-18-2143-template-index-enrich/results/run.log
```

## 4. 结果

| 指标 | E9（基线） | 本次（+caption 类别） | 变化 |
|---|---|---|---|
| Top-1 | 38% | | |
| Top-3 | 43% | | |
| Recall@10 | 56% | | |
| MRR | 0.428 | | |

关键逐条（预期抬升）：
- `梁模板支撑架` → 期望 `011702006 矩形梁`(模板) 进 top-k（嵌入现含"模板及支架"）
- 砼墙模板 `011702011` / 楼梯模板 `011702024` 同理

## 5. 分析与结论

- **对照**：（措施桶 Recall 抬升几条？本体桶有无回归？）
- **结论**：（✅/⛔/🟡 + 一句话）
- **下一步**：若措施桶仍有「本体 vs 模板同名」排序问题 → 把 category 也喂给 structural 重排（查询无模板意图时
  下压 category 含模板的候选）；本体 underspec → sparse 混检。

> 跑完在 `experiments.md` 顶部加 E10 并链回本文件夹。
