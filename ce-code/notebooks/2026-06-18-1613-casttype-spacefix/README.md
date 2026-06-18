# 实验 2026-06-18-1613 · 修 cast_type MinerU 空格 bug 复跑

> 状态：🟡 进行中（待服务器跑）。承接：[[experiments.md E8]]（换源暴露 cast_type 空格 bug）。

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

> 待服务器跑后填。重点看柱本体桶（`010502001`）是否从 miss 翻正。

| 指标 | E8（cast_type 空格 bug） | 本次（修空格后） | 变化 |
|---|---|---|---|
| Top-1 | 38% | | |
| Top-3 | 43% | | |
| Recall@10 | 54% | | |
| MRR | 0.425 | | |

关键逐条（预期翻正）：
- `柱；混凝土强度C40` → 期望 `010502001 矩形柱`（现浇）命中，不再被 `010509001`（预制）压

## 5. 分析与结论

- **对照**：（柱桶翻正几条？有无回归？）
- **结论**：（✅/⛔/🟡 + 一句话）
- **下一步**：若柱桶回正 → 余下主要是「本体 vs 模板同名」（E8 §5.2，从 chapter 附录S 派生 is_measure
  down-rank）+ chapter/caption 脏数据折叠空白。

> 跑完在 `experiments.md` 顶部加 E9 并链回本文件夹。
