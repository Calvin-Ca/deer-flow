# 实验 2026-06-18-1613 · 修 cast_type MinerU 空格 bug 复跑

> 状态：✅ cast_type 修复采纳（预制正确压下、零回归）；但 Top-1 未涨——召回诊断揭示真瓶颈。
> 跑于 2026-06-18，结果见 `results/run.log`。承接：[[experiments.md E8]]。

## 1. 背景与假设

- **问题**：E8 换正确源后，柱本体桶（`010502001 矩形柱`现浇）仍全 miss，被 `010509001 矩形柱`（预制）压。
  根因定位：`010509001` 的 caption 是 `"预 制混凝土柱"`——MinerU 在中文字间插了空格，`cast_type` 的
  `"预制" in cap` 子串匹配漏判 → 预制项没被 down-rank。
- **假设**：`cast_type` 匹配前折叠空白后，预制 caption 能正确判为"预制"，召回期 down-rank 生效，柱本体桶回正。

## 2. 配置

- **代码版本**：commit `<本实验 commit>`
  - `cost/bill_index.py`：`cast_type` 加 `re.sub(r"\s+","",caption)` 去空格归一（+ 测试 `test_cast_type_minerU_spaced_caption`）
- **本次只动的变量**：仅 `cast_type` 去空格（bill_spec、gold、嵌入、structural 全不变）。
- **数据**：collection `cost_bill_spec_kb_2013`（**重建**，cast_type 字段重新派生）/ gold `match_gold_2013.jsonl`（不变，91 条）
- **服务依赖**：Milvus :19530 + embedding bge-large :8097

## 3. 运行脚本

```bash
git pull
bash notebooks/2026-06-18-1613-casttype-spacefix/run.sh 2>&1 | tee notebooks/2026-06-18-1613-casttype-spacefix/results/run.log
```

## 4. 结果

| 指标 | E8（cast_type 空格 bug） | 本次（修空格后） | 变化 |
|---|---|---|---|
| Top-1 | 38% | 38% | = |
| Top-3 | 43% | 43% | = |
| Recall@10 | 54% | 56% | +2% |
| MRR | 0.425 | 0.428 | ~ |
| 平均命中秩 | 2.04 | 2.29 | ↓(更多低秩 gold 进 top10) |

- ✅ **cast_type 修复生效**：`柱；混凝土强度C40` 不再命中 `010509001`(预制)；现浇本体 `010502001` 从
  E8 的 rank—（不在 top10）升到 **rank 9**。预制被正确压下，**零回归**。
- ❌ **但 Top-1 未涨**：柱 Top-1 被 `010507005 扶手压顶`/`011001004 保温柱梁` 抢；`010502001` 只到 rank 9
  ——query 只说"柱"（无"矩形"），dense 在一堆含"柱"项里发散，本体排不到前列。

**召回诊断（本轮关键产出）**：91 条中 **51 命中 top10 / 40 miss**，且 **40 条 miss 全部"在库"**（无一数据缺失）
→ Recall@10=56% 纯属**检索(嵌入)问题**，非覆盖问题。miss 结构：

| miss 类 | 量 | 代表 | 根因 |
|---|---|---|---|
| **模板(0117)码** | ~20 | 011702011 砼墙模板×6 / 011702024 楼梯模板×4 / 011702006 梁模板×2 | **模板码名=结构名、不含"模板"**（E8 §5.2）→ 被本体拽走 |
| **本体 underspec** | ~20 | 010502 矩形柱×5 / 010507 ×3 / 010516 钢筋接头×4 | query 只说"柱/墙"，dense 发散 |

## 5. 分析与结论

- **对照**：cast_type 去空格修复正确（预制压下、010502 进 top10、零回归），但不足以提 Top-1——柱 Top-1 卡在
  「query underspec + dense 短名弱」，非 cast_type 能解。
- **结论 ✅（修复采纳）+ 🔑（定位真瓶颈）**：真瓶颈是 **Recall@10=56% 且 40/40 miss 在库 = 纯检索缺口**。
  按 PRD §6 + E4 认知，Top-1 选码归任务层 LLM（它拿 top10），知识层目标是**提 Recall@10**。
- **下一步（按杠杆排序）**：
  1. **模板索引文本增强**（最大单桶 ~20 条）：从 chapter 附录S 派生措施类别（模板/脚手架/超高），并入
     `bill_embed_text`（让 011702006 索引文本含"模板"），同解「本体 vs 模板同名」。→ 下一个实验。
  2. 本体 underspec（柱/墙）：query 规范化（"柱"→默认矩形柱先验）或 KG 章节约束；或 BM25/BGE-M3 sparse
     混检救召回（40/40 在库 → sparse 的 term 匹配现已justified，E1 的"暂不上 sparse"前提已被真实数据推翻）。
  3. chapter/caption 脏（MinerU 空格）→ 嵌入期统一折叠空白。

> 已在 `experiments.md` 顶部加 E9 并链回本文件夹。

> 跑完在 `experiments.md` 顶部加 E9 并链回本文件夹。
