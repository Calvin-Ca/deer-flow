# 技术方案｜工程量清单计价智能体（CostAgent）

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 对应 PRD | CostAgent PRD v2.0 |
| 状态 | Draft / 待技术评审 |
| 范围 | MVP 技术架构 + 关键子系统详设 + 分阶段实施 |

## 设计原则

1. **能算的不交给模型猜**：几何计算、扣减规则、综合单价公式走确定性引擎；LLM 只负责理解、映射、不确定性决策与自然语言生成。
2. **三层数据底座协同**：关系库（精确查询）+ 知识图谱（关系推理）+ 向量库（语义召回），各司其职。
3. **可追溯、可审计、可介入**：每个产出节点记录推理链与数据来源，HITL 贯穿全流程。
4. **异步 + 流式**：长耗时环节（解析/算量）异步化，进度经 SSE 实时回传。
5. **私有化优先**：模型与数据可全栈内网部署，满足造价数据合规要求。

---

## 一、技术总览

```
┌──────────────────────────────────────────────────────────────┐
│ 前端  Vue 3 + Pinia + 图纸画布(Konva/PDF.js) + 复核工作台        │
└───────────────┬──────────────────────────────────────────────┘
                │ REST + SSE/WebSocket
┌───────────────▼──────────────────────────────────────────────┐
│ 接入层  FastAPI 网关  · 鉴权 · 项目/任务管理 · 文件服务          │
├──────────────────────────────────────────────────────────────┤
│ 编排层  LangGraph StateGraph                                   │
│   节点: parse → quantify → match → price → audit → export      │
│   机制: checkpoint(持久化) + interrupt(HITL) + 条件路由(重算)   │
├──────────────────────────────────────────────────────────────┤
│ 专业 Agent + 工具层                                            │
│   图纸解析Agent | 算量Agent | 清单匹配Agent | 组价Agent | 审核Agent│
│   工具: PDF/DXF/IFC解析器 · 算量引擎 · 混合检索 · 规则引擎 · 计算校验│
├──────────────────────────────────────────────────────────────┤
│ 模型服务  vLLM(多卡)                                            │
│   LLM: Qwen3 系列(推理/生成)   VLM: Qwen2.5-VL(图纸理解)        │
│   Embedding: BGE-M3(dense+sparse 混检)                          │
├──────────────────────────────────────────────────────────────┤
│ 数据层                                                          │
│   关系库 PostgreSQL  规范/定额/价格/历史/项目                    │
│   知识图谱 Neo4j     构件—清单—定额—工料机                       │
│   向量库 Milvus      规范条文/做法/历史案例                      │
│   对象存储 MinIO     图纸原件/中间产物/导出文件                  │
└──────────────────────────────────────────────────────────────┘
```

---

## 二、技术选型总表

| 层 | 选型 | 理由 |
|---|---|---|
| 前端 | Vue 3 + Pinia + Element Plus + PDF.js/Konva | 图纸画布与构件高亮联动 |
| 后端 | FastAPI + SQLAlchemy + Celery/异步任务 | 异步长任务 + SSE 流式 |
| 编排 | LangGraph | 状态机 + checkpoint + HITL interrupt |
| LLM | Qwen3-8B/更大（可切换 GPT/DeepSeek） | 私有化优先国产；多模型路由 |
| VLM | Qwen2.5-VL-7B | 图纸图元/标注/图例理解 |
| Embedding | BGE-M3（dense + sparse） | 单模型支持混合检索 |
| 关系库 | PostgreSQL | 强一致、JSONB 灵活、版本管理 |
| 知识图谱 | Neo4j | 多跳关系遍历（清单→定额→工料机） |
| 向量库 | Milvus | 你已有栈，规模化语义检索 |
| CAD/BIM | ezdxf（DXF）+ IfcOpenShell（IFC）+ PyMuPDF（PDF） | 分级解析 |
| 部署 | Docker + 多卡服务器（stone） | 内网私有化 |

---

## 三、数据层设计

数据是护城河，也是「能算的不交给模型猜」的前提。

### 3.1 关系库（PostgreSQL）

核心表（DDL 草图，省略索引/约束细节）：

```sql
-- 清单规范库（GB50500 + GB50854 系列结构化）
CREATE TABLE bill_spec (
  code            CHAR(9) PRIMARY KEY,   -- 前9位全国统一编码
  name            TEXT NOT NULL,         -- 清单项目名称
  unit            TEXT NOT NULL,         -- 计量单位
  calc_rule       TEXT,                  -- 工程量计算规则(GB50854)
  feature_schema  JSONB,                 -- 项目特征项模板
  chapter         TEXT,                  -- 所属分部
  spec_version    TEXT                   -- 规范版本
);

-- 定额库（分地区/版本）
CREATE TABLE quota_item (
  id            BIGSERIAL PRIMARY KEY,
  region        TEXT NOT NULL,           -- 地区(如 广东2018)
  quota_code    TEXT NOT NULL,           -- 定额子目编号
  name          TEXT NOT NULL,
  unit          TEXT NOT NULL,
  base_price    NUMERIC,                 -- 基价
  labor_cost    NUMERIC, material_cost NUMERIC, machine_cost NUMERIC,
  UNIQUE(region, quota_code, spec_version)
);

-- 工料机（人材机）含量：定额子目 -> 资源
CREATE TABLE quota_resource (
  quota_id     BIGINT REFERENCES quota_item(id),
  resource_id  BIGINT REFERENCES resource(id),
  consumption  NUMERIC NOT NULL          -- 含量
);

-- 资源 + 价格(带时效)
CREATE TABLE resource (
  id BIGSERIAL PRIMARY KEY, name TEXT, spec TEXT,
  category TEXT,                          -- 人工/材料/机械
  unit TEXT
);
CREATE TABLE resource_price (
  resource_id BIGINT REFERENCES resource(id),
  region TEXT, price NUMERIC,
  price_type TEXT,                        -- 信息价/市场价/历史价
  effective_period DATERANGE              -- 时效
);

-- 历史工程库（脱敏，供对标与异常检测）
CREATE TABLE hist_bill (
  project_id BIGINT, bill_code CHAR(9),
  feature JSONB, quantity NUMERIC, unit_price NUMERIC,
  project_type TEXT, region TEXT, completed_at DATE
);
```

> 规范库与定额库为**强一致、可审计**资产，必须带 `version` 与 `region` 维度。

### 3.2 知识图谱（Neo4j）

承载「构件 → 清单项 → 定额子目 → 工料机」的关系推理，这是组价自动化的核心。

```
节点:
  (:ComponentType {name})        构件类型: 墙/梁/板/柱...
  (:BillItem {code, name, unit}) 清单项
  (:Feature {key, value})        项目特征
  (:QuotaItem {code, region})    定额子目
  (:Resource {name, category})   工料机

关系:
  (ComponentType)-[:MAPS_TO]->(BillItem)        构件→清单(多对多)
  (BillItem)-[:HAS_FEATURE]->(Feature)          清单特征项
  (BillItem)-[:APPLIES]->(QuotaItem)            套定额(多对多)
  (QuotaItem)-[:CONSUMES {consumption}]->(Resource)  含量
```

组价时的典型查询（给定清单项，查全部工料机及含量）：

```cypher
MATCH (b:BillItem {code:$code})-[:APPLIES]->(q:QuotaItem)
      -[c:CONSUMES]->(r:Resource)
RETURN q.code, r.name, r.category, c.consumption
```

> 说明：KG 数据由关系库派生/同步而来，KG 负责「多跳关系遍历与推理」，精确数值仍以关系库为准（单一事实来源）。资源有限时，P0 可先用 PostgreSQL 关联表模拟图关系，P1 再上 Neo4j。

### 3.3 向量库（Milvus + BGE-M3 混合检索）

用于规范条文、做法说明、历史案例的语义召回（供清单匹配候选生成与 RAG）。

```
Collection: bill_spec_kb
  fields: code, text(条文/特征描述), dense_vec, sparse_vec, meta(chapter,version)
检索: BGE-M3 同时产出 dense + sparse(类BM25) → 混合召回 + RRF 融合 → rerank
```

### 3.4 数据构建管线

```
GB 规范 PDF ──MinerU/版式解析──> 结构化条目 ──> bill_spec 表
定额电子表 ───导入/清洗──────────> quota_item + quota_resource
信息价文件 ───定期抓取/导入──────> resource_price(带时效)
历史项目 ─────归档+脱敏──────────> hist_bill
上述各库 ─────实体/关系抽取──────> Neo4j KG
规范/案例 ────切分+BGE-M3 Embed──> Milvus
```

---

## 四、图纸解析层（最硬骨头，分级策略）

> **架构修正（BIM 上移为共享底座）**：BIM 模型是横跨建筑全生命周期的共享资产（设计/算量/施工/运维），不是 CostAgent 私有输入。因此 **Tier-1（IFC）的存储/解析/取量上移到独立的 `ce-bim` BIM 底座层**（与 `ce-code` 平级，单一 owner，:8102，暴露按 GlobalId 的 BIM 原语），**CostAgent 降为它的第一个消费方**——`parse` 节点对 IFC 输入改为打 `ce-bim` 原语取"构件 + 基础几何量 + 属性"，再在 `quantify` 叠加造价专有的扣减规则。Tier-2/3（DXF/PDF）仍在本层 `parse_agent`。详见 `ce-bim/PRD.md`。

**核心判断**：2D 施工图直接还原几何/构件语义，是全行业未完全解决的问题（广联达等仍依赖人工翻模）。因此采用**按输入质量分级**的策略，能拿到结构化输入就优先用，纯 PDF 作为兜底且强制人工复核。

| 级别 | 输入 | 工具 | 量的可靠性 | 说明 |
|---|---|---|---|---|
| Tier-1 | BIM / IFC | **`ce-bim` 底座**（IfcOpenShell） | 高 | IFC 自带 `IfcElementQuantity`，几乎可直接取量，**首选**；解析/取量在 `ce-bim`，本层打其原语 |
| Tier-2 | CAD / DWG·DXF | ezdxf | 中高 | 解析图层/块/实体，按图层命名约定映射构件 |
| Tier-3 | 矢量 PDF | PyMuPDF + VLM | 中 | 提取矢量路径+文字标注，重建几何，VLM 辅助理解图例/标注，**强制人工复核** |
| 降级 | 扫描件 | OCR + VLM | 低 | 仅提示，不自动定稿 |

VLM（Qwen2.5-VL）的角色：识别图例、读懂标注/说明文字、判断构件类型、关联分散图纸信息——而**几何尺寸尽量从矢量数据精确提取**，不让 VLM「目测」尺寸。

统一输出结构（喂给算量引擎）：

```json
{
  "drawing_id": "...",
  "components": [{
    "id": "C-001", "type": "wall",
    "geometry": {"length": 3200, "height": 3000, "thickness": 200, "unit": "mm"},
    "level": "F1", "axis": "A-1~A-2",
    "material": "C30", "spec": "...",
    "source": {"page": 3, "layer": "QTW", "bbox": [...]},
    "confidence": 0.82
  }]
}
```

> `confidence` 与 `source` 是 HITL 复核与可追溯的关键：低置信项在前端标红并定位回原图。

---

## 五、工程量计算引擎（确定性）

纯 Python 计算，**不经过 LLM**。两部分：几何计算 + 扣减规则引擎。

扣减规则按 GB50854 配置化，避免硬编码：

```python
# 规则以声明式配置表达，便于按地区/口径调整与审计
RULES = {
  "concrete_slab": {
    "base": "length * width * thickness",
    "deductions": [
      {"when": "opening_area > 0.3",  # 单孔>0.3㎡扣减
       "subtract": "opening_area * thickness"}
    ],
    "unit": "m3"
  },
  "beam": { "base": "...", "deductions": ["梁板相交扣板内梁体积", ...] },
}

def compute(component, rules):
    qty = eval_expr(rules[component.type]["base"], component.geometry)
    for d in rules[component.type]["deductions"]:
        if eval_cond(d["when"], component):
            qty -= eval_expr(d["subtract"], component)
    return Quantity(value=qty, formula=trace, unit=rules[...]["unit"])
```

每个量产出 `formula`（可展开的计算式）与中间值——满足 PRD「计算过程 100% 可核查」的验收标准。复杂相交关系（梁板柱节点扣减）的几何判定是本模块技术重点。

---

## 六、检索与匹配

### 6.1 清单匹配（构件 → 清单项编码）

混合检索 + KG 约束 + LLM 决策的三段式：

```
构件语义(type/material/spec)
  → ① 混合召回: BGE-M3 dense+sparse 在 bill_spec_kb 取 Top-K 候选
  → ② KG 约束: ComponentType-[:MAPS_TO]->BillItem 收窄候选
  → ③ LLM rerank+决策: 选定编码 + 生成项目特征描述 + 给出依据
  → 输出 12位编码(前9位规范 + 后3位顺序码) + 特征描述 + 置信度
```

LLM 在此只做「候选内择优 + 特征描述生成」，候选集由检索+KG 限定，降低幻觉与错配。

### 6.2 组价（清单项 → 综合单价）

KG 驱动、确定性公式收尾：

```
清单项 → ① KG 取套用定额子目(BillItem-[:APPLIES]->QuotaItem)
        → ② 取工料机含量(QuotaItem-[:CONSUMES]->Resource)
        → ③ 关联价格库(按 region + 时效 取 resource_price)
        → ④ 公式计算:
             综合单价 = 人工费 + 材料费 + 机具费 + 管理费 + 利润 + 风险
        → 费用汇总: 分部分项 + 措施 + 其他 + 规费 + 税金
```

其中「一项清单对应多条定额」「定额地区差异」由 KG 的多对多关系 + region 维度处理。

---

## 七、Agent 编排层（LangGraph）

### 7.1 状态定义

```python
class CostAgentState(TypedDict):
    project_id: str
    parsed: list[Component]
    quantities: list[Quantity]
    bill_items: list[BillItem]
    priced: list[PricedItem]
    findings: list[Finding]
    stage: str                  # 当前阶段
    hitl: dict                  # 人工修改回填
    low_conf: list[str]         # 低置信项 id(供前端标红)
```

### 7.2 图结构 + HITL 中断 + 条件路由

```python
g = StateGraph(CostAgentState)
g.add_node("parse",    parse_agent)
g.add_node("quantify", quantify_node)     # 确定性引擎
g.add_node("match",    match_agent)
g.add_node("price",    price_node)        # KG + 公式
g.add_node("audit",    audit_agent)
g.add_node("export",   export_node)

g.add_edge("parse", "quantify")
g.add_edge("quantify", "match")
g.add_edge("match", "price")
g.add_edge("price", "audit")
g.add_edge("audit", "export")

# 每个关键节点后用 interrupt 暂停，等待人工复核
app = g.compile(
    checkpointer=PostgresSaver(...),          # 状态持久化、可恢复
    interrupt_after=["parse","quantify","match","audit"]
)
```

- **HITL**：`interrupt_after` 在节点后暂停，前端展示结果；用户修改经 API 回填 state，`Command(resume=...)` 续跑。
- **条件路由（重算）**：用户在「算量」环节改了构件 → 路由回 `quantify` 重算下游，避免全量重跑。
- **流式**：`app.astream(..., stream_mode="updates")` 的事件经后端转 SSE 推到前端（你熟悉的 SSE 模式）。
- **checkpoint**：长流程可中断恢复，符合造价「改了再算」的工作习惯。

---

## 八、专业 Agent 设计

| Agent | 模式 | 工具 | LLM/VLM 角色 |
|---|---|---|---|
| 图纸解析 | ReAct | PDF/DXF/IFC 解析器、VLM | VLM 理解图例/标注/构件类型 |
| 算量 | 工具调用 | 算量引擎、几何校验 | 仅处理歧义（如标注缺失时推断） |
| 清单匹配 | 检索增强 | 混合检索、KG 查询 | rerank 选码 + 生成特征描述 |
| 组价 | 工具链 | KG 查询、价格匹配、公式计算 | 仅处理定额选择歧义 |
| 审核 | 规则+统计 | 规则引擎、历史对标、异常检测 | 生成可读的风险说明 |

---

## 九、审核引擎

三类检查并行，按严重度分级输出：

```
① 规则校验   : 必算项缺失、编码/特征不合规、单位错误  (确定性)
② 统计异常   : 工程量 / 单价 与 hist_bill 同类项分布对比, 超 Nσ 标记
③ 价格偏离   : 综合单价相对信息价偏离阈值
→ Finding{level: high/mid/low, item_id, reason, evidence}
```

阈值随 `project_type` 调整，避免过度告警（PRD 的「狼来了」风险）。

---

## 十、后端服务（FastAPI）

### 服务拆分

```
gateway        鉴权/项目/任务管理
ingestion      文件上传/解析触发
orchestration  LangGraph 运行与流式
data-service   关系库/KG/向量库访问
model-proxy    vLLM 调用与模型路由
export-service Excel 生成
```

### 关键 API

```
POST   /projects/{id}/drawings          上传图纸 → MinIO
POST   /projects/{id}/run               启动流程 → 返回 task_id
GET    /tasks/{id}/stream               SSE 进度/中间结果
GET    /projects/{id}/{stage}           取某阶段结果(components/quantities/...)
PATCH  /projects/{id}/components/{cid}   HITL 修改 → resume 重算下游
GET    /projects/{id}/export.xlsx       导出清单计价文件
```

长任务异步化（Celery / asyncio 后台任务），进度经 SSE 回传，避免 HTTP 超时。

---

## 十一、前端（Vue 3）

复核工作台核心交互（**按输入类型路由**：IFC 走 3D 模式，矢量 PDF/DXF 走 2D 画布）：

- **双栏联动（2D）**：左侧图纸画布（PDF.js/Konva 渲染原图 + 构件高亮框），右侧结构化结果表；点击任一侧高亮另一侧（按 `bbox`）。
- **双栏联动（3D，IFC 输入）**：左侧 3D 模型用**共享前端包 `ce-bim-viewer`**（web-ifc + Three.js，从 `ce-bim` 拉 IFC 原件客户端渲染），右侧结果表；选择/隔离/剖切/测量/属性面板/空间树导航/**按属性着色**，**以 `GlobalId` 为键**与右侧双向高亮。viewer 是跨产品共享组件（审图/FM 复用），不属 CostAgent 私有，见 `ce-bim/PRD.md §3`。
- **低置信标红**：`low_conf` 项目醒目标记，引导优先复核。
- **就地编辑**：构件/工程量/清单/单价均可改，提交后触发后端重算，SSE 回推新结果。
- **来源可追溯**：每项可展开「依据」——引用的规范条/定额子目/历史对标值。

---

## 十二、模型服务（vLLM 多卡）

部署在多卡服务器（stone），按显存与并发拆分服务，conda/容器隔离：

```
GPU0-1: vLLM serve Qwen3 (LLM, 推理/生成/rerank)
GPU2  : vLLM serve Qwen2.5-VL (图纸理解)
GPU3  : vLLM serve BGE-M3 (embedding, dense+sparse)
```

- **模型路由**：model-proxy 按任务类型分发（生成→LLM，看图→VLM，向量→Embedding），并支持切换到外部 API（GPT/DeepSeek）作为兜底/对比。
- **批处理**：embedding/rerank 走批量推理提吞吐。

---

## 十三、部署与运维

- 全栈 Docker Compose / K8s；数据与模型内网私有化部署满足合规。
- 数据层：PostgreSQL + Neo4j + Milvus + MinIO 容器化。
- GPU：vLLM 服务按上节分卡；显存紧张时 VLM 与 Embedding 可错峰/量化（INT8/FP16）。
- 备份：规范/定额/历史库定期备份并版本归档。

---

## 十四、关键难点与技术取舍

| 难点 | 取舍 |
|---|---|
| 2D PDF → 几何语义极难 | 分级输入(IFC>DXF>PDF)；纯 PDF 强制人工复核；几何走矢量不靠 VLM 目测 |
| 扣减规则复杂且地区差异 | 声明式规则配置 + region 维度；先攻单地区房建 |
| 清单↔定额多对多 | KG 表达关系，检索+KG 收窄候选，LLM 只在候选内决策 |
| LLM 幻觉影响造价准确性 | 计算确定性化；LLM 输出附依据与置信度；红线内只建议不定稿 |
| 价格时效 | 价格带 `effective_period`；组价时按期匹配 + 调差 |
| KG 工程量大 | P0 用 PG 关联表模拟，P1 再迁 Neo4j |

---

## 十五、评测工程

与 PRD 的质量体系对应，落地为可回归的 eval harness：

```
datasets/
  parse_gold.jsonl     图纸→构件 金标准
  qty_gold.jsonl       工程量金标准(人工复核值)
  match_gold.jsonl     构件→清单编码 标注
  audit_inject.jsonl   注入典型错误的回归集
metrics: recall/precision, 相对误差, Top-1/Top-3, 错误召回率
```

每版迭代跑全量评测，结果入库对比，防止能力退化；未达红线的模块默认「只建议不定稿」。

---

## 十六、分阶段技术实施

| 阶段 | 技术目标 | 关键产出 |
|---|---|---|
| M0 数据底座 | 单地区规范/定额/价格库结构化 + KG 原型 + Milvus 入库 | 可查询数据层 + 数据管线 |
| M1 算量打通 | Tier-1/2 输入(IFC/DXF)→构件→工程量 + 评测集 | 算量引擎 + 规则配置 + 人工基线 |
| M2 清单+组价 | 混合检索匹配 + KG 组价 + Excel 导出 | 端到端 MVP 闭环 |
| M3 编排+协同 | LangGraph 全流程 + HITL + Vue 复核台 | 可交付辅助产品 |
| M4 扩展 | 矢量 PDF 解析 + 多专业 + 设计变更联动重算 | P1 能力 |

> 技术上建议 M1 先从 **IFC/DXF 取量** 切入（量最准、最易验证闭环），把「矢量 PDF 直接算量」这个高风险项推到 M4，与 PRD 的 MVP「矢量 PDF」做一处务实调整——否则 MVP 的准确率红线很难达成。
>
> **依赖**：M1 的 IFC 取量依赖 `ce-bim` BIM 底座先立（按 GlobalId 取构件+基础几何量的原语，:8102）；BIM 底座只需先实现 CostAgent 所需原语（YAGNI），进度见 `ce-bim/TODO.md`。3D 复核 viewer（`ce-bim-viewer` 共享包）接在 M3。

---

## 附录：端到端时序

```
用户上传图纸 ─> ingestion 存 MinIO ─> 触发 LangGraph run(task_id)
  parse(VLM+解析器) ──interrupt──> 前端复核构件 ──resume──>
  quantify(规则引擎) ─interrupt──> 前端复核工程量 ─resume──>
  match(检索+KG+LLM) ─interrupt──> 前端复核清单 ──resume──>
  price(KG+公式) ───────────────────────────────────────>
  audit(规则+统计) ──interrupt──> 前端处置风险 ──resume──>
  export ─> 生成 Excel 清单计价文件(含来源/计算式) ─> 下载
        全程: astream 事件 ─> 后端 ─> SSE ─> 前端进度/结果
```
