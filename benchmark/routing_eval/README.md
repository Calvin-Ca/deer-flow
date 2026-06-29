# Agent 集成评测集（方案 0 skill-only 升级判定门）

> 用途：跑 **AGENT_INTEGRATION_DEV.md §0** 的「升级判定门」——在**默认 lead agent（skill-only，agent_name=None）**下逐条提问，量两项指标，决定是否从方案 0 升级方案 A。
> 评测口径：在 **flash / thinking / pro** 三档下做（避开 ultra 的 task 委派双脑歧义，见 DEV §1.2-5）。

## 评测集 `agent_routing_eval.jsonl`

每行一个用例，字段：

| 字段 | 含义 |
|---|---|
| `id` | 用例号（对应 `ce-services/前端测试用例.md` 的 A*/B*） |
| `agent` | 期望命中的能力：`norm-qa`（规范问答）/ `cost-agent`（算量计价） |
| `group` | `no_version`（不带版本，红线主判据样本）/ `with_version`（带版本）/ `boundary`（越界，应拒答不调脚本） |
| `query` | 喂给对话框的原始问法 |
| `expect_route` | 是否**应该去调脚本**（`qa.py`/`cost.py`）。`boundary` 越界用例为 `false`（应识别不支持并拒答） |
| `expect_clarify` | 是否**应该先 `ask_clarification` 反问版本/补描述**。`no_version` 组为 `true` |
| `gold` | 澄清后应使用的 standard/spec（`no_version` 组取第二轮应回的版本）；越界用例为 `null` |
| `note` | 判读提示 / 已知召回缺口归因 |

## 两项指标（§0 升级判定门）

设 R = `expect_route=true` 的用例集，C = `group=no_version` 的用例集。

1. **路由率** = (R 中模型**真去调了 `cost.py`/`qa.py`** 的条数) / |R|
   - 衡量弱模型（Qwen3-8B）能否从渐进披露的 skill 简介里正确识别并调用脚本，而非自己瞎答。
2. **红线遵守率（主判据，安全攸关）** = (C 中模型**真先 `ask_clarification` 反问**版本的条数) / |C|
   - 衡量「不带版本必反问」这条命根子红线在渐进披露下的强制力。版本错 = 串库 = 错价，容错率低，权重高于路由率。

## 判定（§0）

- 两项**均达标** → **停在方案 0**，不做 A/D。
- **红线遵守率不达标** → 升级**方案 A**（常驻 `SOUL.md` 直接补强红线；方案 D 治不到此病根）。
- 仅当瓶颈是**上下文隔离 / 并行重取数**（非红线）→ 才考虑方案 D。

> 达标阈值未在 DEV 文档拍死，建议初判：路由率 ≥ 0.8、红线遵守率 ≥ 0.95（安全攸关从严）。最终阈值由用户结合首轮跑分确认。

## 怎么跑

当前为**人工判读**：四服务起齐（见 `前端测试用例.md` 前置表，:8099/:8100/:8101 + Gateway），在前端对话框逐条贴 `query`，对照 `expect_route` / `expect_clarify` 记录命中，按上面公式算两率。

> 归因速查见 `前端测试用例.md` 第三节：答非所问/召回错构件多为**知识层 ce-code 召回缺口**（非编排 bug）；不反问/不调脚本才是**编排层**问题（调 prompt 或切 qwen-plus 基座）。
