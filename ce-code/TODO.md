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
- ✅ **模板索引文本增强**（E10）：caption 子表类别注入嵌入、门控措施项(0117)，措施桶模板码从"全灭"修到进 top-3。
  **Recall@10 56→60%、Top-3 43→54%、Top-1 持平、零回归**。
- 🔑 真瓶颈仍是召回（miss 多在库的纯检索缺口）；按 PRD §6 Top-1 选码归任务层 LLM，知识层续**提 Recall@10**
  （下一步 sparse 混检 / 本体 underspec）。

---

## 四、造价规范问答检索引擎 —— ⏳ 恢复中（2026-06-22 起）

> **2026-06-22 方向调整**：知识层新增第二能力——**造价规范条文检索**（与组价取数并列）。语料为造价类规范
> （GB 50500/50854/50856、深圳费率/消耗量标准），非防火规范；做法是从 git 历史（`68686329^`）**恢复+适配**
> 被删的 hybrid 检索引擎，而非从零重写。任务层生成侧见 `../ce-services/TODO.md` Norm-QA 章节。

- [x] **A1 恢复引擎 + 接回包结构**（✅ 2026-06-22，import 层）：`git checkout 68686329^` 恢复
  `retrieval/`（bm25/dense/hybrid/rrf/graph/service）+ `ir/{context,feature,query,retrieval}` +
  `index/` + `feature/` + `build.py` + `utils/` + `service/{knowledge_api,retrieve_service}`；
  适配 `ir.{chunk,document,profile}`→`ingest.ir.*`（16处）、build 的 `parser/splitter`→`ingest.*`、
  补恢复漏掉的 `ir/feature.py`。全量 py_compile + 静态 import 解析全绿（运行时第三方依赖/Chunk 接口待服务器验）。
- [x] **A2 验 chunk 兼容**（✅ 2026-06-22，**零适配**）：`ingest.ir.Chunk` 与引擎同期定版（chunk.py 06-15
  定版后只搬家未改），`Chunk.from_dict` 原生读 chunks.json；引擎对 chunk 的属性访问（content/title/
  provenance/node_path/standard_id/parent_id/children_ids/ancestor_*/tables/images/expandable_refs/
  is_grounded）全落现字段集，无 `is_mandatory`（强条 2026-06-12 已降级为 `modal` 表征）。结论：无需改代码。
- [ ] **A3 建索引（服务器）**：chunks.json 已存在 → 跑 `view→feature→index`（跳过 parse/split）。
  - 入口 `build.py`；prereq：embed 服务 :8097（`/model` bge-large-zh-v1.5，与 cost 轨共用，不新部署）+ Milvus :19530。
  - chunks 在 `data/structured/chunks/` 桶 → 命令带 `--structured-dir data/structured/chunks`。
  - collection 自动隔离：`collection_name()` 产 `building_code_*` 前缀，与清单库 `cost_bill_*` 天然不撞。
  - 语料范围待定：优先 GB 50500/50854/50856（计量计价规范条文）；信息价/费率/消耗量更偏数据表，按需再加。
- [x] **A4 检索端点**（✅ 2026-06-22，代码就位待 A3 索引验）：**简化**——不新建 `/norm/search`，复活
  `service/knowledge_api.py` 作 :8100 统一入口（已含 cost_router + /search /expand /clause，是 cost_api 超集）；
  现有通用 `/search` 补 `standard` 别名即服务造价规范。`config.STANDARD_ALIASES` 加 5 部计量计价规范
  （gb50500/50854/50856 × 2013/2024，→ safe_std store 名）。README/DEV 启动改 `python -m service.knowledge_api`。
  collection `building_code_*` 与清单库 `cost_bill_*` 天然隔离。

---

## 待办（按优先级，详见 `notebooks/BACKLOG.md`）

### A. 召回提升（不依赖外部数据，可立即做）
- [x] **模板索引文本增强**（✅ E10，2026-06-18）：caption 子表类别注入嵌入、门控 0117 措施码 → Recall@10 56→60%、
  Top-3 43→54%、零回归。
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
