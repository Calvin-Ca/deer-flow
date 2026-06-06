# ce-code（知识层）· 需求与设计 PRD

> 知识层 = **数据 + 检索**。本文件是 `ce-code/` 的需求/设计上下文，改这一层代码前先读。
> 项目级共享上下文（定位/决策/领域/约定/环境）见根目录 `CLAUDE.md`；进度见同目录 `TODO.md`。

知识层职责：把建筑规范 PDF 解析为**多表征条款库**，并对外只暴露**检索原语**（`/search` `/expand` `/clause`）。retrieval + rerank 模型只在此加载一份（唯一 owner）。**不含生成、不含编排**——那是任务层（`ce-services/`）的事。

---

## 0. 领域铁律与项目约束

**领域铁律（贯穿数据 / 检索全篇）**：
- **强条必须 100% 召回**（漏条 = 合规事故）；强条召回率 > 召回排序，宁可多召不可漏
- 条款有严格层级结构（章/节/条/款/项）和密集交叉引用，**不能按字符切分**
- 强制性（必须/严禁/应）与推荐性（宜/可）必须**显式区分**
- **黑体强条 ≠ 语气强制**：中国规范"强制性条文"是法律 designation（黑体印刷），与语气是两回事；"漏强条=事故"特指漏黑体强条
- **适用性判断是一切**：召回不能赌语义相似，引用图与适用范围索引的权重高于向量索引

**项目约束**：
- MVP 规范范围：**单一规范深做**，首选《建筑设计防火规范》GB 50016-2014(2018)——强条密集、引用复杂、公众高频查询
- 数据源：**PDF**，且**解析质量是项目天花板**
- **双轨知识资产**：知识层服务三个 agent（规范问答 / 算量组价 / 图纸审核，见 §4）。① **规范问答/审图轨**——防火规范条款库（GB 50016，多表征，见 §1–§4）；② **算量组价轨（CostAgent）**——造价知识底座（清单计量规范 GB 50500/GB 50854 + 定额 + 价格 + 历史 + 知识图谱，见 §5），数据源不止 PDF，还含定额电子表 / 信息价文件 / 历史项目。两轨共用同一检索引擎与多表征思路，差异在数据资产与检索原语。

**风险与红线（数据/检索侧）**：

| 风险 | 应对 |
|---|---|
| PDF 解析质量是天花板 | 阶段 0 POC 不达标不进入后续阶段 |
| 漏召强条 = 合规事故 | 强条召回率盯死，宁可多召回不可漏 |
| 多版本混淆 | 元数据强制带 version + status，废止条款不参与召回 |
| 规范版权 | 规范 PDF 商业使用前必须确认版权状况；`data/` 不进 git |
| 造价数据非强一致（造价轨） | 规范库/定额库带 version + region 强一致可审计；价格库带时效标签（`effective_period`） |
| 能算的交给模型猜（造价轨） | 几何/扣减/综合单价走任务层确定性引擎，知识层 LLM 仅在检索候选内择优决策（见 §5.5） |

---

## 1. 数据层 — 结构化抽取（不要朴素分块）

**条款树**：解析为 章 → 节 → 条 → 款 → 项 的层级，每个叶子节点是一个独立 chunk。

**每条款元数据 schema（v2 精化版，当前真值）**——区分黑体强条/语气、引用分型+双向、适用范围谓词、祖先链：

```python
{
    "standard_id": "GB 50016-2014(2018)",
    "version": "2018",
    "effective_date": "2018-10-01",
    "status": "active",                  # active / superseded / abolished（条款级）
    "clause_path": "5.3.4",
    "ancestor_titles": ["5 建筑分类和耐火等级", "5.3 防火分区和层数"],  # 祖先标题链（chunk 上下文）
    "modal_strength": "应",               # 语气：必须/严禁/不应/不得/应/宜/可
    "is_mandatory_clause": True,          # 是否黑体强制性条文（法律强制；召回率盯死的是它，≠ 语气）
    "applicable_scope": {                 # 结构化适用范围 → 条件召回（算量/审图/合规的桥）
        "building_types": ["住宅"],
        "height_range_m": [27, 54],
        "area_range_m2": [...],
        "conditions": [                   # 散文里的隐含/复杂条件抽成谓词
            {"field": "height_m", "op": ">", "value": 27},
            {"field": "underground", "op": "==", "value": False}
        ],
        "scope_status": "extracted"       # extracted / unknown（抽不准→标 unknown 进保守召回，宁可多召）
    },
    "references": [                        # 引用边：分型 + 方向（替代旧的扁平 references_to）
        {"to": "5.2.1", "type": "strong"},          # strong=应符合(必拉+继承强制性)
        {"to": "6.1.2", "type": "weak"},            # weak=参见(可选拉取)
        {"to": "GB 50116 3.1.1", "type": "cross_standard"}  # 跨规范→触发多规范召回
    ],                                    # 另有 type="exclude"(本条不适用于X)：禁止正向扩展
    "referenced_by": ["5.3.1"],          # 反向边
    "content": "...",
    "tables": [...],                     # 关联表格 ID（结构化可查询、继承条款强制性）
    "formulas": [...],                   # LaTeX + 变量语义/单位（供阶段 3 sandbox 执行）
    "figures": [...]                     # 原图 + VLM 描述（描述入向量库可语义召回）
}
```

> v1 旧 schema（扁平 `is_mandatory` + `references_to`）已废弃。关键差异：`is_mandatory` → 拆 `modal_strength` + `is_mandatory_clause`；`references_to` → `references`（分型）+ `referenced_by`（反向）；新增 `ancestor_titles` / `conditions` / `scope_status` / `formulas`。

**表格/公式/图示**单独抽取存储，关联回所属条款：表格 → 结构化 JSON（或 markdown）；公式 → LaTeX；图示 → 原图 + VLM 生成的描述。

---

## 2. PDF 解析流水线（最高风险环节，~60% 工程量）

| 任务 | 推荐 | 备选 |
|---|---|---|
| 版面分析 + 整体解析 | **MinerU**（国产，中文规范效果最好） | Marker、Unstructured |
| 表格抽取 | Camelot / pdfplumber | MinerU 自带 |
| 公式抽取 | MinerU / Mathpix | — |
| 图示理解 | **Claude Opus 4.7 vision**（高分辨率自动启用） | GPT-4o |
| 条款树解析 | 正则 + 规则（自写） | 无现成方案 |
| 引用图构建 | 正则 + LLM 校验（自写） | — |

数据文件位置（服务器，不进 git）：`data/raw/`（PDF）、`data/parsed/`（MinerU 输出）、`data/structured/`（条款库 JSON）、`data/vector_store/`（BM25 + Milvus 索引）。

---

## 3. 检索层 — 混合检索 + 引用扩展

```
Query
  ↓ 查询改写（LLM 生成 3-5 个变体，含同义词、专业术语）
  ↓
并行召回：
  ├─ BM25（精确匹配条文号、专业术语）
  ├─ 向量（语义召回）
  └─ 元数据过滤（standard / version / chapter / 适用范围）
  ↓ 合并 + 去重
  ↓ 引用图扩展（命中 A → 自动拉取 A 引用的 B、C；仅 strong 边）
  ↓ Rerank（cross-encoder；强条优先保留，不被截断）
返回 top-k 条款（带元数据）
```

**技术栈（已确定；服务地址/版本/dim 见 `DEV.md` 依赖表，此处只列选型）**：
- 嵌入：`bge-large-zh-v1.5`（vLLM 服务）
- 向量库：**Milvus**（MilvusClient API，collection 名只含字母/数字/下划线）
- BM25：**rank-bm25**（字符级分词，无需 Elasticsearch）
- Rerank：`bge-reranker-large`（FlagEmbedding，可选；不可用时自动 fallback 到 RRF 排序）

**检索硬性约束**：
- **强条召回率 > 召回排序**：宁可多召回 10 条无关，也不能漏 1 条相关强条
- 引用扩展默认开启（命中条款的所有 strong `references` 必须一并拉取）
- 元数据过滤优先于向量排序（先按 standard/version/scope filter，再排）

**检索验收标准（评测契约；写检索代码前先建 30–50 条评测集）**：

评测集落 `data/eval_set/`（入 git），单条用例格式：

```json
{
  "query": "24m 高的住宅楼疏散楼梯最小宽度是多少？",
  "expected_clauses": ["GB 50016-2014(2018) 5.5.30", "5.5.31"],
  "must_be_mandatory": true,
  "user_type": "通用咨询"
}
```

核心指标（达标线随阶段细化，进度见 `TODO.md`）：
- **强条召回率**（Recall@k on mandatory clauses）—— **首要**
- 引用条款召回率（被引用的关联条款是否被拉取）
- 误报强条率（把推荐性当强制性的比例）
- 适用性误判率（Phase B 谓词数据后启用）

---

## 4. 知识层设计：多表征条款库（核心）

> 三个目标 agent（规范问答 / 算量组价 / 图纸审核）共享同一知识层，差异只在输入适配与计算逻辑。其中**算量组价 agent（CostAgent）**除本节多表征条款库外，还需 §5 的造价知识底座（关系库 + 知识图谱 + 价格/定额数据）。

**核心判断：知识层的主资产不是 embedding，是"条款的结构化表征"。** 建筑规范两条铁律——*漏强条 = 合规事故*、*适用性判断是一切*——决定了召回不能赌语义相似。**引用图 与 适用范围索引 的权重高于向量索引**；embedding 只是众多召回入口里最不可靠的一个。

知识层 = **多表征条款库**：同一批条款，四种并存表征，检索是四者的可组合并集。

| 表征 | 载体 | 召回作用 | 主要场景 |
|---|---|---|---|
| ① 文本 | 向量 + BM25 | 语义 / 条文号·术语精确 | 问答 |
| ② 树 | 章→节→条→款→项 | 层级上下文 | 所有场景 |
| ③ 引用图 | references（分型+双向） | 引用扩展（强条不漏） | 所有场景 |
| ④ 条件 | 结构化适用范围谓词 | 条件匹配召回 | 算量 / 审图 / 合规 |

**资产建模要点（易踩坑，按优先级；schema 见 §1）：**

- **【P0】黑体强条 ≠ 语气强制**：拆 `modal_strength`（必须/严禁/应/宜/可）与 `is_mandatory_clause`（规范正式**黑体字**标注）。中国规范"强制性条文"是法律 designation（黑体印刷），与语气是两回事；"漏强条=事故"特指漏黑体强条。解析须保留 MinerU 版面分析给出的**字重信息**。
- **【P0】引用边分型 + 双向**：`strong`（应符合→必拉、继承强制性）/ `weak`（参见→可选）/ `exclude`（本条不适用于 X→**禁止正向扩展**，否则反引）/ `cross_standard`（触发多规范召回）。"命中 A 自动拉引用"只对 strong 无条件执行；同时存 `referenced_by` 反向边。引用图即规范的知识图谱，后续 GraphRAG 的底座。
- **【P0】表格可查询**：大量强制要求（防火间距/耐火极限/疏散宽度）在表格里、不在正文。表格 → 结构化 JSON，支持"给定行列条件取值"，继承所属条款强制性；不能只当图片或纯文本。对算量/审图尤其关键。
- **【P1】适用范围谓词抽取**：把散文条件（"建筑高度大于 54m 的住宅…"）抽成结构化谓词，是算量/审图的桥，也是工程量最大的天花板。抽不准的标 `scope_status: unknown` 进**保守召回**（宁可多召不可漏）；评测集盯"适用性误判率"。
- **【P1】chunk 携带祖先链**：叶子款/项向量化时拼上 `ancestor_titles` + 所属"条"全文（contextual / small-to-big）；召回时既给命中款、也给完整条。
- **【P2】条款级版本/效力**：`status`/`version`/`effective_date` 到条款粒度（局部修订只改部分条）；废止条款不参与召回但保留（可回答"何时废止"）。多规范从一开始就支持（GB 50116 待收录）。

**检索面（四通道，对应四表征）：**
```
Query → ① 向量(语义) + ① BM25(条文号/术语) + ④ 适用范围结构化过滤
      → 合并去重 → ③ 引用图扩展(仅 strong 边) → rerank(强条不截断)
      → 返回(命中款 + 完整条 + 强制性 + 引用)
※ ④ 适用范围过滤优先于排序：先按 standard/version/scope 圈定范围，再在范围内排。
```

---

## 5. 造价知识底座扩展（CostAgent / 算量组价 agent）

> §1–§4 是**防火规范条款库**（服务规范问答 / 图纸审核）。本节是**算量组价 agent（CostAgent）**的数据/检索需求：把施工图最终转化为「工程量清单 + 组价」，知识层需在条款库之外新增**造价数据资产**，并从"多表征条款库"扩展为**三层知识底座**。
> 边界：施工图解析、算量引擎（几何 + 扣减规则）、Agent 编排、Excel 导出属**任务层**（`../ce-services/`），不在知识层。知识层只负责造价**数据资产 + 检索/匹配原语**。整体方案另见根目录 `cost_agent_prd.md` / `cost_agent_tech.md`。

### 5.1 三层知识底座

造价两条铁律——*能算的不交给模型猜*、*清单↔定额是多对多关系*——决定了纯向量库不够用，需三层协同（关系库为单一事实来源，KG 由其派生）：

| 层 | 载体 | 职责 | 数值真值 |
|---|---|---|---|
| 关系库 | PostgreSQL | 规范/定额/价格/历史的精确查询，强一致，version + region 维度 | ✅ 在此 |
| 知识图谱 | Neo4j（**P0 可用 PG 关联表模拟，P1 再迁 Neo4j**） | 「构件→清单项→定额子目→工料机」多跳关系推理，组价核心 | 由关系库同步 |
| 向量库 | Milvus（BGE-M3 dense+sparse 混检） | 规范条文/做法/历史案例语义召回，供清单匹配候选生成 | — |

### 5.2 造价数据资产

| 数据资产 | 内容 | 来源 | 构建方式 | 更新机制 |
|---|---|---|---|---|
| 清单规范库 | GB 50500 计价 + GB 50854 计量规范结构化条目（编码、计量单位、计算规则、特征项模板） | 国标 | MinerU 解析 + 规则结构化入库 | 随标准修订人工维护 |
| 定额库 | 国家/地区定额子目、人材机含量、基价 | 各地定额电子表 | 导入/清洗 | 按地区/年份版本管理 |
| 价格库 | 信息价 / 市场价 / 历史成交价 | 造价信息平台、历史项目 | 定期抓取/导入 | 按期（月/季）更新，带时效 |
| 历史工程库 | 已完成项目的清单、量、价、构件特征（脱敏） | 内部沉淀 | 项目归档 + 脱敏 | 随项目持续积累 |
| 知识图谱 | 构件—清单—定额—工料机关系网络 | 上述各库 | 实体抽取 + 关系建模 | 随底层库同步 |
| 向量知识库 | 规范条文、做法说明、历史案例语义向量 | 规范 + 历史库 | 切分 + BGE-M3 入库 | 增量更新 |

> **强一致、可审计**：规范库与定额库必须带 `version` + `region`；价格库带时效标签；历史库需脱敏与质量标注。

### 5.3 关系库核心表（DDL 详见 `cost_agent_tech.md` §3.1）

`bill_spec`（清单规范，9 位统一编码 + calc_rule + feature_schema + spec_version）、`quota_item`（定额子目，region + base_price + 人材机费，UNIQUE(region, quota_code, spec_version)）、`quota_resource`（定额→资源含量）、`resource` + `resource_price`（资源价格，带 `effective_period` 时效 DATERANGE）、`hist_bill`（历史工程，供对标与异常检测）。

### 5.4 知识图谱 schema（详见 `cost_agent_tech.md` §3.2）

```
节点: (:ComponentType) 墙/梁/板/柱  (:BillItem code,name,unit)  (:Feature)
      (:QuotaItem code,region)       (:Resource name,category)
关系: (ComponentType)-[:MAPS_TO]->(BillItem)        构件→清单（多对多）
      (BillItem)-[:HAS_FEATURE]->(Feature)
      (BillItem)-[:APPLIES]->(QuotaItem)            套定额（多对多）
      (QuotaItem)-[:CONSUMES {consumption}]->(Resource)  工料机含量
```

### 5.5 造价检索/匹配流程（两段，均为「检索/KG 收窄候选 + LLM 候选内决策」）

```
① 清单匹配（构件 → 12 位编码）
   构件语义(type/material/spec)
     → 混合召回: BGE-M3 dense+sparse 在 bill_spec_kb 取 Top-K 候选
     → KG 约束: ComponentType-[:MAPS_TO]->BillItem 收窄候选
     → LLM rerank + 决策: 选码 + 生成项目特征描述 + 给依据
     → 输出 12 位编码(前9位规范 + 后3位顺序码) + 特征描述 + 置信度

② 组价取数（清单项 → 工料机含量 + 价格）—— 数值由任务层确定性公式收尾
   清单项(+region)
     → KG 取套用定额(BillItem-[:APPLIES]->QuotaItem)
     → 取工料机含量(QuotaItem-[:CONSUMES]->Resource)
     → 关联价格库(按 region + 时效取 resource_price)
     → 返回工料机清单 + 含量 + 价格(综合单价公式在任务层算)
```

LLM 在此只做「候选内择优 + 特征描述生成」，候选集由检索 + KG 限定，降低幻觉与错配。「一项清单对应多条定额」「定额地区差异」由 KG 多对多关系 + region 维度处理。

### 5.6 造价侧红线

- **强一致可审计**：规范库/定额库带 version + region；价格带 `effective_period` 时效，组价按期匹配 + 调差。
- **能算的不交给模型猜**：几何/扣减/综合单价公式走任务层确定性引擎；知识层 LLM 只在检索候选内择优 + 生成特征描述，附依据与置信度。
- **红线内只建议不定稿**：编码匹配 Top-1 ≥ 85%、定额套用准确率 ≥ 85% 等准确率红线达成前，对应输出默认只建议、必须经人工确认（HITL 在任务层）。

---

## 6. 知识服务端点（:8100，`service/server.py`）

**只暴露检索原语**，被任务层（`ce-services/`）和 skill 以纯 HTTP 客户端方式调用：

```
POST /search            裸检索（条款 + meta）          核心，给 qa/算量/审图复用
POST /expand            对 clause_path 做引用扩展
GET  /clause/{std}/{path}  单条款直取
GET  /health            含 ready_standards / vector_store / deps 地址
```

**待补原语（规范轨）**：`/filter`（适用范围过滤）、`/rerank` 依赖 Phase B 谓词数据，后补（见 TODO.md）。

**造价原语（算量组价轨，Phase C，依赖 §5 造价数据资产入库）**：
```
POST /bill/match              构件特征 → 清单项候选（混合召回 + KG 约束）   清单匹配
POST /price/compose          清单项(+region) → 工料机含量 + 价格（KG + 价格库）  组价取数
GET  /quota/{region}/{code}  定额子目直取
```

**代码组织与目录结构见 `README.md`**（实现细节随重构变动，不在 PRD 维护）：`retrieval/`（纯检索引擎库）/ `service/server.py`（知识服务 :8100）/ `scripts/`（薄 CLI）。

---

## 7. 模型与生成约定（知识层用到的）

> 选型与职责见下；**服务地址、model_id、dim、版本约束统一以 `DEV.md` 依赖表为准**，此处不重复，避免漂移。

- **Embedding（规范轨）**：`bge-large-zh-v1.5`（条款/query 向量化）
- **Embedding（造价轨）**：`BGE-M3`（dense + sparse 单模型混检，对应 §5.1 向量库），按 `cost_agent_tech.md` 选型；与规范轨是否统一为单一 embedding 服务待评估（开放项）
- **rerank**：本地 `bge-reranker-large`（FlagEmbedding）或 RRF fallback
- **VLM（图示理解）**：`Qwen2.5-VL-7B`（PDF 解析时图示理解）
- 生成/编排不在知识层（见 `ce-services/PRD.md`）
