# ce-code（组价知识库）· 进度 TODO

> ce-code = **深圳房建组价知识库**。需求见 `PRD.md`、设计见 `DEV.md`、实验记录见 `notebooks/`。
> 任务层进度见 `../ce-services/TODO.md`。
>
> **2026-06-18 重构**：移除规范条文检索 RAG（防火 GB 50016 轨停做），ce-code 收窄为纯组价知识底座；
> 代码重组为 `ingest/`（摄取）+ `cost/`（组价核心）+ `service/cost_api` + `tools/`。防火轨历史靠 git 追溯
> （tag `pre-cost-refactor`），不再在此罗列。

---

## 一、数据底座 —— ✅ 端到端可用

PG `ce_cost`（:5433）已灌齐，组价取数链通（清单→定额→工料机→价）：

| 数据 | 表 | 状态 |
|---|---|---|
| 清单（计量规范） | `bill_spec` | ✅ 2216 条：2024 房建 472 + 安装 1183 + 2013 房建 561（spec 版本隔离共存） |
| 定额（SJG 消耗量） | `quota_item` / `resource` / `quota_resource` | ✅ SJG171 建筑 + SJG170 土方（1257 子目 / 8278 含量 / 991 资源） |
| 信息价（月刊） | `resource_price` | ✅ 2026-05（1138 条，带时效） |
| 费率（2023） | `fee_rate` | ✅ 24 条 |
| 费用构成（50500） | `price_composition` | ✅ 综合单价 6 项 / 工程造价 4 部分 |
| 清单→定额映射 | `bill_quota_map` | ✅ 313 边 / 53 清单（名称匹配 P0，带 bill_spec_version 版本隔离） |
| 资源↔信息价对齐 | `resource_price_map` | ✅ 43 确定性边（同物异名归一 + 单位换算） |

取数原语（:8100，`spec` 必填）：`/bill/match`（清单召回）、`/price/compose`（组价）、`/quota`（定额直取）——
均服务器实测通过。

## 二、国标版本严格隔离 —— ✅ 全栈闭环（2026-06-18）

2013/2024 同 9 位码不同义，混用串库。隔离三层 + 服务器验证零回归：
- **关系库**：`bill_spec` 复合主键 `(code, spec_version)`；`bill_quota_map` 加 `bill_spec_version`。
- **向量库**：`cost_bill_spec_kb`(2024) / `cost_bill_spec_kb_2013` 分库。
- **API/路由**：`config.SPEC_REGISTRY` + `resolve_spec`；`/bill/match`、`/price/compose` spec 必填（未知→400，
  2013 组价数据未就绪→501）。`bill_index --spec` 防建库混版本。
- **任务层**：`ce-services/common/cost_client` 透传 spec（CostAgent 调用前提示用户选版本）。

## 三、/bill/match 召回质量 —— 🟡 瓶颈=召回（详见 notebooks E6–E9 + BACKLOG）

- ✅ 2013 清单源换正确的 **GB-50854-2013（房建计量规范）**，措施码对齐真实项目（解开"同码不同义"死结）。
- ✅ 现浇/预制消歧（cast_type，含 MinerU 空格 bug 修复），零回归。
- 🔑 **真瓶颈**：真实 gold Recall@10 偏低，且 miss 多在库 → 纯检索缺口。按 PRD §6，Top-1 选码归任务层 LLM，
  知识层目标转向**提 Recall@10**。

---

## 待办（按优先级，详见 `notebooks/BACKLOG.md`）

### A. 召回提升（不依赖外部数据，可立即做）
- [ ] **模板索引文本增强**（最大单桶）：模板码（0117）名不含"模板"二字 → 从附录S caption 派生措施类别并入嵌入，
  同解"本体 vs 模板同名"。
- [ ] 本体 underspec / **BM25 或 BGE-M3 sparse 混检**（real data Recall 已 justify sparse）；先复用 rank-bm25。
- [ ] chapter/caption 脏数据（MinerU 插空格）嵌入期统一折叠空白。
- [ ] 造价评测集扩充（真实结算 gold），量化 Top-1/Top-3 稳数字。

### B. Phase 2 —— 2013 全功能组价（卡数据）
- [ ] 收 2013 真实项目**实际采用的定额版本 + 价格时点**（口径：不能用 SJG-2024 套 2013 清单，见 BACKLOG）。
- [ ] 建 2013 清单→定额映射；`bill_quota_map` 已备版本维度，数据齐后翻 `SPEC_REGISTRY` 2013 `supports_compose=True`
  即生效（代码不改）。
- [ ] 安装类 03 清单（防雷接地等）待收 GB 50856-2013。

### C. 知识图谱 / 映射富化
- [ ] `bill_quota_map` 名称匹配仅覆盖 ~11%（53/472）→ 补语义召回 / 章节对齐 / 专家标注提覆盖。
- [ ] `MAPS_TO`（构件→清单）待 BIM 底座 `../ce-bim/` 接入后建（算量侧）。
- [ ] P1：迁 Neo4j 多跳遍历（清单→定额→工料机）。

### D. 数据精修
- [ ] 定额单位 `$m^3$` LaTeX 残留（`quota.py` 抽取遗留，污染 quota_item/resource 的 unit）→ 清洗后重抽 SJG。
- [ ] SJG `chapter`/`ancestor_titles` 偏弱（无规整目录），入库后抽查。
- [ ] 历史工程库 `hist_bill`（脱敏 + 质量标注，供相似案例对标；[可缓]）。

### E. 规范条文检索（已移除，按需重建）
- [ ] 如需"查计价规则原文"，以干净模块重建条文检索（不复活旧 RAG）；当前 `price_composition` 结构化表已覆盖
  费用构成查询需求。
