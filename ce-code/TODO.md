# ce-code（知识层）· 进度 TODO

> 知识层（数据 + 检索）的执行进度与重构历程。需求/设计见同目录 `PRD.md`；任务层进度见 `../ce-services/TODO.md`。

---

## 阶段 0：技术 POC（✅ 已完成）

MinerU 解析 + 条款树提取 + 质量审核，在 GB 50378-2006 和 GB 50016 上验证通过。

## 阶段 1：检索 MVP（✅ 已完成）

- [x] GB 50016 PDF → MinerU 解析（`split_and_parse.py` 分块 80 页/块，`hybrid-auto-engine` 后端，`CUDA_VISIBLE_DEVICES=2`）
- [x] 安装 retrieval 依赖（pymilvus、rank-bm25、requests；`uv add` 写入 `pyproject.toml`）
- [x] `04_build_index.py`：建 BM25 + Milvus 向量双索引（GB 50016 已建，**911 条条款**）
- [x] `05_retrieve.py`：混合检索 + 引用扩展（BM25 + 向量 + RRF 合并 + 引用图扩展 + Rerank）
- [x] 评测集 `data/eval_set/gb50016_eval.json`（45 条用例）；`07_eval.py` 评测已跑
- [x] 向量索引建立后补充 `flush` 确保数据落盘

### 评测集（写检索代码前先建 30-50 条）

用例格式（`data/eval_set/`）：
```json
{
  "query": "24m 高的住宅楼疏散楼梯最小宽度是多少？",
  "expected_clauses": ["GB 50016-2014(2018) 5.5.30", "5.5.31"],
  "must_be_mandatory": true,
  "user_type": "通用咨询"
}
```

**核心指标**：
- 强条召回率（Recall@k on mandatory clauses）— **首要**
- 引用条款召回率（被引用的关联条款是否被拉取）
- 误报强条率（把推荐性当强制性的比例）
- （Phase B 后）适用性误判率

---

## 重构历程（行为保持，不改 schema、不重建索引）

### Phase A：检索引擎收敛（✅ 2026-06-01）

把检索逻辑从 importlib 反向加载的 POC 脚本收敛进 `retrieval/` 包。

- [x] 建 `retrieval/`（`config.py` + `engine.py`，从 `05_retrieve.py` 搬）→ 05 改薄（保留 `retrieve` 名兼容）
- [x] 知识服务 `server.py` 接 retrieval + 拆原语端点 `/search` `/expand` `/clause`
- [x] **服务器验证**：`07_eval.py` 召回率与重构前一致（行为保持核心证据）

**痛点（重构前，已消除）**：06/10 用 `importlib` 按 `05_retrieve.py` 文件名反向加载 → 改名即崩；store-dir 解析 / collection 命名 / DEFAULTS 在多处重写。

### v3：知识层瘦身（✅ 2026-06-03）

任务层迁出后，知识服务只留检索原语。

- [x] `service/server.py` 删 `/qa` `/retrieve` 端点 + generation 依赖，只留 `/search` `/expand` `/clause` `/health`
- [x] `service/` 现仅剩 `server.py`（generation/orchestration/params/queries 已迁至 `ce-services/`）
- [x] 退役删除 POC CLI `06/08/09/10`；保留 `05_retrieve.py` / `07_eval.py`（只依赖 retrieval）
- [x] **行为等价**：`/search` 内部 `bm25_top_k = vector_top_k = top_k*2` 调 `retrieval.engine.search`，与重构前 orchestration 直调参数逐字一致 → `07_eval.py` 召回率不变

### Docker（✅ 2026-06-04）

- [x] `docker/ce-code/`：知识服务镜像（pytorch 基底，含 GPU/FlagEmbedding，~6GB）+ compose（仅 :8100）
- [x] `network_mode: host` 直连宿主机 Milvus/vLLM

> 部署注记：后台起服务**勿用 `nohup`**（stone 服务器 `Exit 125` 静默失败），改用 `setsid` 或 tmux；诊断"起不来"先前台直跑看真实报错。

---

## Phase B：数据模型改造（⬜ 待办，下一步优先级）

> 需改 `02_extract` schema + **重建索引**。与前述结构重构解耦。这四块决定三个 agent（问答/算量/审图）的能力天花板。

- [ ] **黑体强条标注**：拆 `modal_strength` / `is_mandatory_clause`，解析保留 MinerU 字重信息
- [ ] **引用边分型 + 双向**：`strong`/`weak`/`exclude`/`cross_standard` + `referenced_by` 反向边
- [ ] **表格结构化可查询**：表格 → JSON，支持"给定行列条件取值"，继承条款强制性
- [ ] **适用范围谓词抽取**：散文条件 → 结构化谓词；抽不准标 `scope_status: unknown` 进保守召回
- [ ] **条款级版本/效力**：`status`/`version`/`effective_date` 到条款粒度
- [ ] 新增检索原语 `/filter`（适用范围过滤）、`/rerank`（依赖谓词数据）
- [ ] 评测集增加"适用性误判率"指标

**多规范扩展**：GB 50116 待收录（当前 `火灾自动报警系统` 维度在 GB 50016 无对应强条）。

---

## Phase C：造价知识底座（CostAgent / 算量组价 agent）（⬜ 待办）

> 对应 PRD §5、`cost_agent_prd.md` 八 / `cost_agent_tech.md` 三、六，以及 CostAgent M0 数据底座里程碑。新增**关系库 + 知识图谱**两层与造价检索原语；与 Phase B（防火轨数据模型）解耦，可并行。
> 范围：**单地区房建**先行（与 CostAgent MVP 一致）；算量引擎/图纸解析/编排在任务层，不在此。

### 数据资产（关系库优先）

- [ ] 关系库 PostgreSQL 建表：`bill_spec` / `quota_item` / `quota_resource` / `resource` / `resource_price` / `hist_bill`，强制带 `version` + `region`，价格带 `effective_period`
- [ ] GB 50500 + GB 50854 清单计量规范结构化入 `bill_spec`（复用 MinerU 解析 + 规则，含 calc_rule + feature_schema）
- [ ] 单地区定额库导入 `quota_item` + `quota_resource` + `resource`（定额电子表清洗）
- [ ] 价格库导入 `resource_price`（信息价/市场价，带 `effective_period` 时效）
- [ ] 历史工程库 `hist_bill`（脱敏 + 质量标注，供审核轨对标）

### 知识图谱

- [ ] **P0**：用 PG 关联表模拟「构件→清单→定额→工料机」关系（`MAPS_TO` / `APPLIES` / `CONSUMES`），跑通组价取数
- [ ] **P1**：迁 Neo4j，多跳遍历（清单→定额→工料机）

### 向量库 + 检索原语

- [ ] 造价 `bill_spec_kb` collection（BGE-M3 dense+sparse 混检），供清单匹配候选生成
- [ ] 新增 `/bill/match`（构件→清单候选：混合召回 + KG 约束 + LLM 决策）
- [ ] 新增 `/price/compose`（清单项+region→工料机含量+价格：KG + 价格库）
- [ ] 新增 `/quota/{region}/{code}`（定额子目直取）

### 造价评测集

- [ ] 清单编码匹配：`match_gold.jsonl`（构件→编码标注），指标 Top-1 ≥ 85% / Top-3 ≥ 95%
- [ ] 定额套用：对照已结算项目，定额套用准确率 ≥ 85%
- [ ] 红线门禁：未达准确率红线的原语默认「只建议不定稿」（HITL 在任务层兜底）

**模型/部署待评估项**：造价轨 embedding 用 BGE-M3 vs 复用规范轨 bge-large-zh-v1.5 是否统一为单服务（见 PRD §7）。
