# 零件级评测（不占 L1~L7 层号）

> 这两个集测的是**单个业务零件的输出质量**，不属于七层框架里的任何一层（七层测的是编排系统：路由/门控/检索/答案/拆解/任务/NFR）。零件对不对是这里量，零件被编排用对没用对在 L6 量。

| 目录 | 测什么 | 条数 | 断言口径 |
|---|---|---:|---|
| `listing_eval/` | **列清单抽取**：设计说明文本 → 清单项列表（构件名/关键特征/工程量） | 7 | `expected[].must_include`（特征必含）+ `quantity`/`no_quantity`（尺寸数字≠工程量）+ `forbidden`（如砂浆不得单独拆条）负向断言 |
| `critic_eval/` | **清单核对 Critic**：原文 + 草表 → 漏项/特征不全/量疑点 | 3 | `expected_findings`（type=missing_item/weak_feature/quantity_doubt）+ `forbidden`（假漏项=误报罚项）+ 负向样本（完整草表期望零质疑） |

两集均为合成种子（含前端真实输入回归样本与实测踩坑难例，note 里有归因），无独立 runner——历史上由 ce-services 工具驱动（已退役），待列清单/核对能力在 backend 内嵌链路稳定后按同套断言重建驱动。
