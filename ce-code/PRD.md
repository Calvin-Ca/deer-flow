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

**风险与红线（数据/检索侧）**：

| 风险 | 应对 |
|---|---|
| PDF 解析质量是天花板 | 阶段 0 POC 不达标不进入后续阶段 |
| 漏召强条 = 合规事故 | 强条召回率盯死，宁可多召回不可漏 |
| 多版本混淆 | 元数据强制带 version + status，废止条款不参与召回 |
| 规范版权 | 规范 PDF 商业使用前必须确认版权状况；`data/` 不进 git |

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

**技术栈（已确定）**：
- 嵌入：`bge-large-zh-v1.5`（vLLM 服务 localhost:8097，model_id=/model，dim=1024，max_len=512）
- 向量库：**Milvus**（localhost:19530；MilvusClient API，collection 名只含字母/数字/下划线）
- BM25：**rank-bm25**（字符级分词，无需 Elasticsearch）
- Rerank：`bge-reranker-large`（FlagEmbedding，可选；不可用时自动 fallback 到 RRF 排序）

**检索硬性约束**：
- **强条召回率 > 召回排序**：宁可多召回 10 条无关，也不能漏 1 条相关强条
- 引用扩展默认开启（命中条款的所有 strong `references` 必须一并拉取）
- 元数据过滤优先于向量排序（先按 standard/version/scope filter，再排）

---

## 4. 知识层设计：多表征条款库（核心）

> 三个目标 agent（规范问答 / 算量组价 / 图纸审核）共享同一知识层，差异只在输入适配与计算逻辑。

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

## 5. 知识服务端点（:8100，`service/server.py`）

**只暴露检索原语**，被任务层（`ce-services/`）和 skill 以纯 HTTP 客户端方式调用：

```
POST /search            裸检索（条款 + meta）          核心，给 qa/算量/审图复用
POST /expand            对 clause_path 做引用扩展
GET  /clause/{std}/{path}  单条款直取
GET  /health            含 ready_standards / vector_store / deps 地址
```

**待补原语**：`/filter`（适用范围过滤）、`/rerank` 依赖 Phase B 谓词数据，后补（见 TODO.md）。

**代码组织**：
```
ce-code/
├── retrieval/          纯检索引擎库（依赖轻，不含 click/rich）
│   ├── config.py       DEFAULTS / STANDARD_ALIASES / resolve_store_dir / collection_name
│   └── engine.py       召回原语 + search() + get_clause()
├── service/
│   └── server.py       知识服务 :8100（import retrieval）
└── scripts/            05_retrieve.py / 07_eval.py 等薄 CLI（import retrieval）
```

---

## 6. 模型与生成约定（知识层用到的）

- **Embedding**：`bge-large-zh-v1.5`（vLLM `http://localhost:8097`，model_id=`/model`）
- **rerank**：本地 `bge-reranker-large`（FlagEmbedding）或 RRF fallback
- **VLM（图示理解）**：`Qwen2.5-VL-7B`（vLLM `http://localhost:8098`，model_id=`/model`）
- 生成/编排不在知识层（见 `ce-services/PRD.md`）

> 模型完整清单、Thinking 切换、服务器环境见根 `CLAUDE.md` §5/§10。
