---
name: cost-agent
description: 算量计价 CostAgent 技能。接收构件/做法的自然语言描述，召回清单候选 → Qwen3 在候选内选定 9 位清单编码 → 取定额工料机含量 + 信息价单价（组价取数）。覆盖深圳房建组价（清单计量国标 GB 50854 2013/2024 双版隔离）。适用于"C30现浇矩形柱组价""某砌体墙套什么清单码"等构件→清单码→价的查询。强制：选码只在候选内不造码、低置信转人工复核、缺价不杜撰、不组装综合单价（不算钱）。
---

# 算量计价 CostAgent（cost-agent）

## 能力

对构件/做法描述做端到端「选码 + 组价取数」：
- **候选召回**：构件描述 → 知识服务 `bill_match` 召回清单候选（向量检索）
- **LLM 选码**：Qwen3 在候选内选定 9 位清单编码，三条红线 + 代码侧确定性兜底（不造码、低置信转人工、空候选转人工）
- **组价取数**：选中码 → 定额工料机含量 + 信息价单价（未命中价的资源标 `no_source`，不杜撰）

> ⚠️ **不算钱**：P1 只到「选码 + 组价取数」，**不组装综合单价/含税造价**（那是确定性公式，P2 阶段）。

## 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--description` / `-d` | 必填 | 构件/做法自然语言描述（如 "C30现浇混凝土矩形柱"） |
| `--spec` | **必填（无默认）** | 国标版本 `2013` / `2024`；2013/2024 错版会串库，须先确认 |
| `--region` | `深圳` | 地区（用于信息价/定额取数） |
| `--top-k` | `10` | 清单候选召回数 |
| `--service-url` | `http://localhost:8101` | 任务服务地址（环境变量 `COST_AGENT_URL`） |
| `--output` | stdout | 结果 JSON 写入路径（可选） |

> ⚠️ **版本红线**：清单计量国标 2013/2024 同 9 位码不同义（如现浇/预制编码段在两版间整体平移），版本错了就串库。
> 调用前**必须**和用户确认按哪版国标组价——若用户没说版本，先反问，不要替他猜默认。
> **2013 组价数据未就绪**：传 `--spec 2013` 只返回选码、`price_status` 标"未就绪"，不出价。

## 架构（重要）

本 skill 是一个**薄 HTTP 客户端**。真正的逻辑跑在服务器上的常驻服务里，分两层：
- **任务服务**（`ce-services/main.py`，默认 `http://localhost:8101`）——本 skill 打 `/cost/compose`（选码 + 组价编排）。
- **知识服务**（`ce-code/service/knowledge_api.py`，默认 `http://localhost:8100`）——提供 `bill_match` / `price_compose` 取数原语；任务服务 HTTP 调它。

`cost.py` 只用 Python 标准库 urllib 转发请求——**沙箱内零第三方依赖，无需 venv、无需向量索引数据、无需 POC 脚本**。

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| **任务服务** | localhost:8101 | 本 skill 调用的 HTTP 服务（选码+组价，/cost/compose） |
| **知识服务** | localhost:8100 | bill_match / price_compose 取数原语（被任务服务调用） |
| Milvus | localhost:19530 | 清单向量库（被知识服务使用） |
| vLLM embedding | localhost:8097 | 文本 embedding（被知识服务使用） |
| vLLM Qwen3-8B | localhost:8099 | LLM 选码（被任务服务使用） |

> 服务启动方式（服务器上，常驻；需先起知识服务再起任务服务）：
> `cd ce-code && uv run python -m service.knowledge_api`（知识服务 :8100）
> `cd ce-services && uv run python main.py`（任务服务 :8101）

## 调用方式

**通过 bash 工具调用 `cost.py`**（用系统 `python3` 即可，无需特定 venv）：

```bash
# 基本组价（spec 必填）
python3 /mnt/skills/public/cost-agent/cost.py \
  --description "C30现浇混凝土矩形柱" \
  --spec 2024

# 保存结果到文件后读取
python3 /mnt/skills/public/cost-agent/cost.py \
  --description "MU10标准砖240厚实心砖墙M5水泥砂浆" \
  --spec 2024 \
  --region 深圳 \
  --output /tmp/cost_result.json \
&& cat /tmp/cost_result.json
```

## 输出格式

```json
{
  "description": "C30现浇混凝土矩形柱",
  "spec": "2024",
  "region": "深圳",
  "candidates_count": 8,
  "selection": {
    "code": "010502006",
    "confidence": 0.95,
    "reason": "选码依据……",
    "need_review": false,
    "alternatives": ["..."]
  },
  "code": "010502006",
  "price": { "...": "定额工料机含量 + 信息价单价 + 小计（未命中价的资源标 no_source）" },
  "price_status": "ok",
  "meta": {"elapsed_ms": 1234, "top_k": 10}
}
```

字段含义：
- `selection.need_review = true` → **低置信/空候选/造码兜底**，须转人工复核，**不要当定稿用**
- `code = null` → 选不出码（已转人工），`price` 为 null、`price_status` 为 `skipped(need_review)`
- `price_status`：`ok`（出价）/ `skipped(need_review)`（没选出码）/ `未就绪(...)`（2013 组价数据未就绪，只有选码）
- `price` 内未命中信息价的资源标 `no_source` —— 这是**透传缺口**，不要替它编价

## 使用原则

1. **选码不造码**：只接受候选内编码，`code` 为 null 或 `need_review=true` 时如实转达"需人工复核"，不强给
2. **版本不猜**：用户没说国标版本（2013/2024）就反问，绝不替他选默认
3. **缺价不杜撰**：`no_source` / `price_status` 非 ok 时如实说明缺口，不补编价格
4. **不算钱**：只给选码 + 工料机含量取数，明确告知"综合单价/含税造价需后续 P2 确定性公式组装"
5. **HITL**：`need_review=true` 的结果只作建议，提示用户人工确认编码

## 常见错误排查

> 这些都是**服务端配置问题**，agent 在沙箱内无法补救（不要尝试建 venv、装包或拷脚本）。
> 应把错误原文转达用户，由用户在服务器侧处理。

| 错误信息 | 原因 | 处理（在服务器上） |
|---|---|---|
| `无法连接服务` | 8101 任务服务未启动 | `cd ce-services && uv run python main.py` |
| `未知国标版本` | `--spec` 不是 2013/2024 | 用 2013 或 2024；确认按哪版国标组价 |
| 返回 400（spec 相关） | 知识服务侧 spec 未知/未配 | 检查 ce-code `config.SPEC_REGISTRY`、确认 spec 合法 |
| 返回 404 | 选中码在该地区无组价数据（定额/价缺） | 正常缺口，如实转达；可换地区或确认该码可组价 |
| 返回 503 | 知识服务 :8100 或 LLM :8099 不可达 | 起知识服务 / `curl http://localhost:8099/health` |
| 返回 502 | LLM 选码输出非合法 JSON | Qwen3 输出异常，重试或检查 :8099 |
| `price_status=未就绪` | 传了 `--spec 2013`（组价数据未就绪） | 预期行为，只出选码；要出价用 `--spec 2024` |
