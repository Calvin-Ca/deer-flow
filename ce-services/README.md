# 任务层 · Norm-QA + CostAgent + 路由编排（ce-services）

知识层（`../ce-code`，默认 `:8100` + `:8102`）的**任务服务**。任务层是知识服务的**纯 HTTP 客户端**——不 `import
retrieval`、不连 Milvus / PG，只打知识服务原语，再叠加路由 / 编排 / 生成 / 选码 / 校验闸逻辑。
共进程 :8101（REST + 内部任务实现）。当前不把 :8101 作为 agent 可见 MCP 工具入口。

> 需求/设计见 `PRD.md` 与仓库根 `AGENT_PRD.md`；服务间契约见 `INTERFACE_CONTRACTS.md`；
> HITL 设计见 `HITL_DESIGN.md`。
> 服务器运行级联调步骤见 `RUNTIME_E2E_RUNBOOK.md`。

## 拓扑（四层骨架）

```
ce-code RAG 服务 :8100 (service.rag_api：条文检索 /search/clause /expand/clauses /clause
                        + bill_match / 半结构化投影检索；MCP「ce-rag」)
ce-code DB  服务 :8102 (service.db_api：/bill /quota /price/query /price/compose /fee-rates /aux-table；
                        PG 真值唯一 owner；MCP「ce-db」)
        ▲ HTTP（knowledge_client / cost_client）
        │
ce-services 任务服务 :8101（REST：/route /norm/qa /cost/compose /cost/session/*；MCP 前门停用）
  ① 前置路由 routing/prerouter    确定性能力分流+形态判定（零 LLM）＋EH-03 跨地域出界检测
  ② 能力层   norm/（检索+带引用生成，零召回→FR-K07 联网兜底三道闸→仍无则拒答给出路）
             cost/（选码+组价取数 compose · HITL 13步图 · /cost/check 清单核对 FR-C v1）
             price（编排器内确定性取数 FR-I，打 /price/query）
  ③ 校验闸   common/guards + norm/guards + cost/guards（C-01/02/03，meta.guard，非 LLM）
  ④ 复合编排 routing/orchestrator（32b 拆解→子任务回①→派发→32b 综合，降级安全）
```

## 端点（:8101，全部经 `main.py` 挂载；`GET /health` 动态列全）

| 端点 | 职责 |
|---|---|
| `POST /route` | ① 前置路由（确定性，无 LLM）：能力分流 + 形态 + clarify 裁定 + EH-03 出界 |
| `POST /orchestrate` | ④ 前门：单一直派 / 复合拆解-综合；子结果带 `meta.guard` |
| `POST /norm/qa` | 规范条文检索 + 带引用作答；零召回→联网兜底（Tier-2 降级标注）→仍无则拒答给出路 |
| `POST /cost/compose` | 构件→候选→LLM 选码→组价取数；`spec` 可缺省（默认深圳·2013，`meta.caliber` 口径声明） |
| `POST /cost/check` | FR-C v1：BOQ 行确定性核对（编码/单位/名称/合价算术；漏项等 v1 诚实 unsupported） |
| `POST /cost/unit-price` `/cost/rollup` | 确定性算钱原语（pydantic 闸门，LLM 不算钱） |
| `POST /cost/session/start·resume·rewind` `GET …/state` | 可中断 HITL 组价会话（langgraph 图 + provenance；缺 spec 默认 2013 不停闸） |

## 启动（服务器）

全栈需 **3 个进程**：RAG 服务 :8100 + DB 服务 :8102（先起）+ 任务服务 :8101。

```bash
cd ce-code     && uv run python -m service.rag_api         # ① :8100 ce-rag（必须先起）
cd ce-code     && uv run python -m service.db_api          # ② :8102 ce-db（必须先起）
cd ce-services && uv sync && uv run python main.py         # ③ :8101 任务服务
```

调用示例：
```bash
curl -s -X POST http://localhost:8101/norm/qa -H 'Content-Type: application/json' -d '{"query":"满堂脚手架工程量怎么计算","standard":"gb50854-2024","top_k":10}'
curl -s -X POST http://localhost:8101/cost/compose -H 'Content-Type: application/json' -d '{"description":"C30现浇混凝土矩形柱"}'
curl -s -X POST http://localhost:8101/orchestrate -H 'Content-Type: application/json' -d '{"query":"深圳本月HRB400钢筋信息价是多少"}'
curl -s -X POST http://localhost:8101/cost/check -H 'Content-Type: application/json' -d '{"rows":[{"code":"010401002","unit":"m3","quantity":100,"unit_price":500,"amount":49000}]}'
```

> 后台常驻**勿用 nohup**（服务器 Exit 125 静默失败），用 `setsid` 或 tmux。

## 配置（env 覆盖，见 common/config.py）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BCRAG_RAG_URL` | `http://localhost:8100` | ce-rag 地址 |
| `BCRAG_DB_URL` | `http://localhost:8102` | ce-db 地址 |
| `BCRAG_KNOWLEDGE_URL` | `http://localhost:8100` | 兼容别名；未单独设 RAG/DB 时回退单入口 |
| `BCRAG_LLM_URL` / `BCRAG_LLM_MODEL_ID` | `http://localhost:8099` / `qwen3-8b` | 桶 A 8b（生成/澄清） |
| `BCRAG_ORCH_LLM_URL` + `BCRAG_ORCH_LLM_MODEL_ID` | 成对回落 8b | 桶 B 32b（拆解/综合/选码消歧），成对设才升 |
| `CE_COST_DEFAULT_SPEC` | `2013` | 组价缺省国标版本（PRD C-05 唯一默认口径；2013 组价数据就绪前 compose 如实 501，过渡可切 2024） |
| `CE_NORM_WEB_FALLBACK` | `1` | 规范问答联网兜底开关（无外网设 0，回落零召回拒答） |
| `CE_HITL_TAU_HIGH` / `CE_HITL_TAU_LOW` | 沿用 `CE_HITL_CONFIDENCE_TAU`(0.75) / `0.60` | 双阈值门控（PRD §4.4 三段式） |

## 红线（输出侧）

- **缺版本不反问，默认深圳·2013**（§4.0/C-05）：任务层补默认 + `meta.caliber` 口径声明；知识层边界 spec 仍必填
- **选码 `need_review`**：低置信选码只建议不定稿，转 HITL 人工复核（三段式门控 τ_high/τ_low）
- **`no_source` 不杜撰**：未命中信息价的工料机/价目透传缺口，不编价
- **他省口径体面告知（EH-03）**：组价/价格严格锁深圳，出界不取数、给建议渠道
- **联网仅 FR-K**：规范问答零召回才走三道闸兜底（服务端确定性执行、Tier-2 硬标注）；组价/价格联网调用=0
- **LLM 不算钱**：组价/单价/汇总/价差为确定性公式（pydantic 闸门），LLM 只做选码/拆解/综合/生成

## 自测（离线，无需服务/LLM）

```bash
uv run python -m routing.prerouter      # 27 例
uv run python -m tools.prerouter_eval   # 金标 24/24（能力分流+口径策略+出界）
uv run python -m routing.orchestrator   # 8 例（stub 注入）
uv run python -m norm.web_fallback      # 16 例（三道闸离线）
uv run python -m norm.guards            # 9 例
uv run python -m cost.guards            # 8 例
uv run python -m cost.check             # 9 例
```
