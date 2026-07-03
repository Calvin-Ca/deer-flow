# Agent 集成评测集（方案 0 skill-only 升级判定门）

> 用途：跑 **AGENT_INTEGRATION_DEV.md §0** 的「升级判定门」——在**默认 lead agent（skill-only，agent_name=None）**下逐条提问，量两项指标，决定是否从方案 0 升级方案 A。
> 评测口径：在 **flash / thinking / pro** 三档下做（避开 ultra 的 task 委派双脑歧义，见 DEV §1.2-5）。

## 评测集 `agent_routing_eval.jsonl`

每行一个用例，字段：

| 字段 | 含义 |
|---|---|
| `id` | 用例号（对应 `ce-services/前端测试用例.md` 的 A*/B*；C*=价格 FR-I、D*=核对 FR-C 为 T9 后新增） |
| `agent` | 期望命中的能力：`norm-qa`（规范问答）/ `cost-agent`（算量计价）/ `price`（价格取数）/ `cost-check`（清单核对） |
| `group` | `no_version`（不带版本）/ `no_feature`（特征缺）/ `with_version`（带版本）/ `boundary`（越界拒答）/ `out_of_scope`（EH-03 他省口径体面告知）/ `web_fallback`（FR-K07 联网兜底）/ `session_sticky`（EH-05 会话粘性）/ `context_check`（FR-C）等 |
| `query` | 喂给对话框的原始问法 |
| `expect_route` | 是否**应该去调能力**（脚本/MCP 工具）。`boundary`/`out_of_scope` 用例为 `false`（应拒答/体面告知，不取数） |
| `expect_clarify` | 是否**应该先 `ask_clarification` 反问**。**T9-1 后口径分侧**：norm 缺口径→会话内**首次** `true`（EH-05）；cost 缺版本→`false`（默认深圳·2013，不反问）；cost 缺**特征**→`true`（EH-04，只问特征） |
| `gold` | 应使用的 standard/spec（cost 缺版本时为默认 `2013`）；越界/出界用例为 `null` |
| `note` | 判读提示 / 已知召回缺口归因 |

## 指标（§0 升级判定门 + T9 口径策略回归）

设 R = `expect_route=true` 的用例集。

1. **路由率** = (R 中模型**真去调了对应能力** 的条数) / |R|
   - 衡量弱模型能否正确识别并调用能力，而非自己瞎答。
2. **口径红线遵守率（主判据，安全攸关，分侧）**：
   - **cost 不反问率**（`group=no_version` 的 B 组）：缺版本**不反问**、默认深圳·2013 且首答带口径声明的比例（T9-1 行为反转后的新红线；反问=违例）；
   - **norm 首次反问率**（`group=no_version` 的 A 组）：会话首次缺口径真反问的比例；
   - **会话粘性达成率**（`group=session_sticky`）：同会话第二问**不再反问**的比例（EH-05）。
3. **出界告知率**（`group=out_of_scope`）：他省口径体面告知（不取数、不给深圳数据冒充）的比例（EH-03/C-02）。
4. **联网兜底呈现合规率**（`group=web_fallback`）：降级标注头 + URL/访问日期完整保留的比例（FR-K07 Tier-2）。

## 判定（§0）

- 两项**均达标** → **停在方案 0**，不做 A/D。
- **红线遵守率不达标** → 升级**方案 A**（常驻 `SOUL.md` 直接补强红线；方案 D 治不到此病根）。
- 仅当瓶颈是**上下文隔离 / 并行重取数**（非红线）→ 才考虑方案 D。

> 达标阈值未在 DEV 文档拍死，建议初判：路由率 ≥ 0.8、红线遵守率 ≥ 0.95（安全攸关从严）。最终阈值由用户结合首轮跑分确认。

## 怎么跑

当前为**人工判读**：四服务起齐（见 `前端测试用例.md` 前置表，:8099/:8100/:8101 + Gateway），在前端对话框逐条贴 `query`，对照 `expect_route` / `expect_clarify` 记录命中，按上面公式算两率。

> 归因速查见 `前端测试用例.md` 第三节：答非所问/召回错构件多为**知识层 ce-code 召回缺口**（非编排 bug）；不反问/不调脚本才是**编排层**问题（调 prompt 或切 qwen-plus 基座）。
