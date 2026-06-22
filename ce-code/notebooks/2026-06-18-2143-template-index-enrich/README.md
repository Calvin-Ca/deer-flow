# 实验 2026-06-18-2143 · 模板索引文本增强（caption 子表类别注入嵌入）

> 状态：🟡 v1(broad) 混合结果（措施桶 Top-3 +10% 但广播注入误伤本体）→ v2(gated) 限定措施项复跑。
> 承接：[[experiments.md E9]]（召回诊断：40/40 miss 在库，一半是措施码）。

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

### v1 — broad（caption 类别注入**所有**清单项）

| 指标 | E9 基线 | v1 broad | Δ |
|---|---|---|---|
| Top-1 | 38% | 34% | **-4%** |
| Top-3 | 43% | **53%** | **+10%** |
| Recall@10 | 56% | **58%** | +2% |
| MRR | 0.428 | 0.437 | +0.009 |
| 平均命中秩 | 2.29 | **1.87** | ↑ |

- ✅ **措施桶召回修好**：模板码现能召回——`梁模板支撑架` gold `011702006`→`011702005 基础梁` rank 2
  （E9 有梁板 rank 4）；`板模板`→`011702006 矩形梁` rank 3；脚手架 `011701006`→`011701007` rank 2。
- ❌ **误伤本体**：`柱 010502001` 从 E9 rank 9 **掉出 top-10**（全是扶手压顶/保温柱梁/填充墙）。广播给本体注
  category（"现浇混凝土柱"≈name+cast_type，无新信息却扰动排序）。Top-1 -4% 亦此 + 模板族内部难定首位。

### v2 — gated（category 仅注入措施项 0117）

> 假设：本体不注入 → 保措施 gain、复原本体桶。`run.sh` 已改为 gated 版重建+重测。

| 指标 | E9 基线 | v1 broad | v2 gated |
|---|---|---|---|
| Top-1 | 38% | 34% | （待跑） |
| Top-3 | 43% | 53% | |
| Recall@10 | 56% | 58% | |

## 5. 分析与结论

- **对照**：v1 证实 caption 类别对**措施码**是关键修复（name 不含"模板"），但广播到**本体码**是冗余噪声、扰动
  本体排序。按知识层成功口径（Recall/Top-3，Top-1 归 LLM），v1 已是净赢（Top-3 +10%）；但本体 recall 回退可避免。
- **结论 🟡（采纳方向，待 v2 收口）**：category 注入限定措施项（0117）。
- **下一步**：v2 跑完看本体是否复原 + 措施 gain 是否保住；之后「本体 vs 模板同名」「模板族内部 Top-1」属排序，
  归任务层 LLM / 后续 structural 用 category。

> 跑完 v2 在 `experiments.md` 顶部加 E10 并链回本文件夹。

> 跑完在 `experiments.md` 顶部加 E10 并链回本文件夹。
