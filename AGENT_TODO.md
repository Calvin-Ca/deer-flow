# 智能体主线 TODO（根目录总控）

> **定位**：跨层主线总控——只回答「现在处于什么阶段、下一步做什么、谁先谁后」。
> 层内细节各归其位：知识层 `ce-code/TODO.md`、任务层 `ce-services/TODO.md`、评测规范 `benchmark/AGENT_BENCHMARK.md`、
> 实现/决策记录 `AGENT_DEV.md`、需求权威 `AGENT_PRD.md`。本文件只挂结论与验收口径，不重复实现细节。
> **本版**：2026-07-06 重写——四个里程碑（M1 基线加固 / M2 批量列清单 / M3 Critic+多智能体证伪 / M4 评测闭环）已收官，
> 记录架构对账结论 + 两条收尾线的接手动作 + 后续主线。

---

## 0. 当前阶段一句话（2026-07-07）

**M4 正式收官**：工程闭环全部立住（确定性路由→组价状态机→金标→CI→行为回归→调参有据），且 **M2 全链
在真实覆盖率（26.9%）下首次通关**（前端 18 步到真总价 819,487.24，算术对账零误差，见 §2②）。**产品闭环
剩一块**：真实用户流量 = 0（评测基建就位、缺子弹）——下一步内网试用，剩余阻塞见 `ce-services/TODO.md`
内网试用 checklist（切生产态重建 + 生产态回归 + 运维三查）。

---

## 1. 架构对账定案（「workflow 顶层 + agents 底层」蓝图 vs 代码，2026-07-05 验收）

蓝图七步管线（解析→门控→组价→Critic→HITL 闸→算钱→导出）**骨架全部落地**，分工铁律四条全部成立：

| 蓝图 | 代码落点 | 判定 |
|---|---|---|
| ① 解析：清单生成 Agent | `ce-services/cost/listing.py` | 🟡 已入籍（信封/只抽不造/source_text⊆原文/降级/金标五守则全齐），但是 **v0 单次调用**，迭代式（分段读→检索验证→自查遗漏）是既定升级项，信封契约不变 |
| ② 门控：代码判定 | `cost/gates.py` + `selection.py` | ✅ τ=0.60/0.30，调参有据（margin 主导、公式三参不动，τ=0.25 待金标 30+） |
| ③ 组价 ×N | `cost/graph.py` 多构件外层循环 | 🟡 循环有、**并行无**（langgraph Send API 可做，等单件质量稳）；件内是 LLM 窄点非 agent（蓝图防坑条：选码不值得 agent 化） |
| ④ Critic | `cost/critic.py` | ✅ 质疑信封随 state 进评审表；P1/P2/P3 三方案实测 96%=96%=96% **证伪多智能体增益**，有据定位人审辅助 |
| ⑤ HITL 闸 | gates=图节点+interrupt；`review-table.tsx` + `session.batch_resume` | ✅ 代码渲染、批量审，不靠 agent 记得问人 |
| ⑥ 算钱 | `cost/pricing.py` 纯函数 | ✅ 无 LLM |
| ⑦ 导出 | `cost/export.py` | 🟡 CSV 有，计价软件格式未做 |

铁律：workflow 拥控制流（`graph.py`「弱模型不驱动流程」）✅ / 降级归代码 ✅ / checkpoint+审计事件 ✅ / agent 间只经 state 不互聊 ✅。

**诚实残留**：前门以内是纯 workflow-top；前门以外**第一跳仍是 lead agent 皮**，靠 `RouteContextMiddleware`
（第零跳收权）+ prompt 红线收窄裁量，服从率由行为回归量化（见 §2①）。终态可选前门直连 UI。

**差的半步**（演进项非缺口）：listing 升迭代式（唯一够格 agent 化的节点）；③ 并行 fan-out。都不动图的契约。

---

## 2. 两条收尾线（下次开工从这里接手；服务器无后台任务在挂，跑批已结束）

### ① 行为回归 —— v3b 全量定案（2026-07-06 深夜）：v2 三大簇全出清，新形态三修落码待 v4

**v3b 矩阵**：route 21/24=87.50%（未挂分 10：A9 + 9 条正确止步于反问）、clarify 30/33=90.91%。
**v2 三大顽疾全部治愈**：E2/E3/E6 哑火出清（收编+观测双修生效）、B2 转绿（先问特征再止步）、A9 按定案挂 —。
剩余 3+3 全是新形态，逐条归因（/route 判定本地实证）+ 三修已落码：
- **A4/B8 反向过问（不该问却问）**：判定均 clarify=null（A4 版本齐 caliber_complete=true、B8 特征齐
  feature_complete=true），模型自作主张反问——上轮「反问必须走工具」红线把 ask_clarification 提得太显眼、
  缺对向约束 → reminder 双修：工具红线限定「clarify 非空」情形 + 补「clarify=null＝禁止反问，直接执行，
  查不到如实说无，不许用反问拖延」。
- **B11 前门口径定案（合规改判）**：模型把出界请求交 ce-task_orchestrate、由编排层确定性出界闸拒绝并正确
  转述话术——比模型自答更稳（弱模型不驱动流程）。runner `expect_route=False` 违规口径改为只计直调取数工具
  （`_is_fetch_tool`：ce-cost_*/qa.py/cost.py，才有杜撰他省数据风险），前门放行；金标 B11 note 写回；
  工具列表去重（call+result 重影）。
- **E5 引导后哑火（唯一余留，不阻塞）**：clarify=feature，orchestrate listing 抽 0 件、引导文本返回后模型
  纯文本收尾——「有工具活动不收编」按设计不介入（E3 的 v3 形态漂移）。**A4/B8 修好后 clarify 32/33=97%
  已过 ≥0.95 门**。治本挂 follow-up：ce-services 0 件引导返回带结构化 need_input 标记 → 中间件识别后对
  该形态放行收编（勿用「工具后仍收编」的宽松版，会干扰组价 HITL 会话话术）。
**待办（服务器，按序单行）**：
- [ ] 重传金标（B11 note）：`cd /mnt/nvme/calvin/code/deer-flow && set -a && . ./.env && set +a && uv run --project backend python benchmark/runner/upload_datasets.py --only routing`
- [ ] 跑 v4：`uv run --project backend python benchmark/runner/run_routing_experiment.py --run-name m4-behavior-v4`
- [ ] 拉矩阵：`uv run --project backend python benchmark/runner/dump_run_scores.py --run-name m4-behavior-v4`——
  预期 A4/B8 不再过问、B11 按新口径转绿、clarify ≥0.95 过门；E5 若仍红属已知余留不阻塞。
（用户口径：benchmark 优化可后置，不阻塞主线。）

<details>
<summary>v2/v3 归因过程存档（定案已并入上文，展开看推理链）</summary>

### 行为回归 v2 —— 逐条矩阵已拉，归因定案（2026-07-06），治理已落码待 v3 验证

**v2 矩阵**：route 24/27=88.89%、clarify 30/34=88.24%。**A1/A2/A7 全部转绿——prompt 治得动 caliber
反问，不必下沉**。剩余失败三簇归因定案：
- **E2/E3 哑火（工具=[]）**：本地实证 /route 对 E2/E3/E4 判定**完全一致**（cost+clarify=feature），
  8B 对同一指令服从随查询词形漂移（「这个项目」「设计说明」把模型带进纯文本索要材料轨、E4 裸关键词
  老实调工具）→ **prompt 治不动实锤，按通则③下沉编排层**：`RouteContextMiddleware` 新增 after_model
  哑火收编——判定要求反问 + 本轮零工具活动 + 文本像在提问 → 确定性转 `ask_clarification` 工具调用
  （链尾 ClarificationMiddleware wrap_tool_call 必拦截 interrupt）；非疑问陈述句不硬转、告警出声。
  reminder 同步加红线「反问一律走 ask_clarification 工具，禁止纯文本反问」。单测 13 条新增
  `backend/tests/test_route_context_middleware.py`。
- **A9 结构性冤判（该调没调 ×1 + 反向过问）**：runner 每条独立 thread 冷启动，EH-05 前提「A1 已确认
  口径」不存在，agent 反问反而正确——金标 `expect_*` 改挂 null（自动分不计），以前端连续同会话人工判读
  为准（本就在 §4 小尾巴）。
- **B2 该问没问（route ✓ clarify ✗）**：光看工具列表裁决不了（可能是前门 orchestrate 接管了特征反问
  ——那是口径盲区非漏问）；`dump_run_scores.py` 已加「答复摘要」列，v3 跑完看它说了什么再定。
**v3 半程定案（2026-07-06 当晚）**：run 在第 13 条崩了（Langfuse items 新建在前，只跑了
E7/G1-G3/E1-E6/D1/C3 这 12 条，A1–C2 没跑到；崩因没留下——runner 原无单条兜底，一条异常杀全轮）。
半程收获与三修：
- ✅ E1/E2/E4/E5 反问全绿（v2 哑火重灾区 E2 转绿），G1-G3/E7/D1 保持绿——收编方向有效。
- **E6 冤案双修**：① 模型 `<think>` 推理块漏进 content，思维链里的「？」误触发收编、整段思维链被当
  question 呈给用户 → 收编前先剥 `<think>…</think>`（含流截断未闭合形态），剥空则不收编；② 收编产生的
  反问只以 ToolMessage 流出、runner 只数 ai tool_calls 看不见（记「工具=[]」）→ runner 补数
  `type=tool` 工具结果名。单测扩到 16 条。
- **runner 补单条 try/except**：跑挂出声跳过不挂分，一条崩不再废整轮（v3 就是这样丢了 22 条），
  下轮崩因直接可见。
- **E3 新形态待口径**：agent 调了 orchestrate（route ✓），listing 0 件引导返回后纯文本收尾——
  「已有工具活动不收编」按设计不介入。是改金标（编排层已接管引导）还是要求引导也走
  ask_clarification，看 v3b 全量矩阵再定。
**待办（服务器，按序单行；无需重起 gateway，runner 嵌入式 git pull 即新代码）**：
- [ ] 单测：`cd /mnt/nvme/calvin/code/deer-flow/backend && PYTHONPATH=. uv run pytest tests/test_route_context_middleware.py -v`（16 条）
- [ ] 重传路由金标（A9 改判；已传过可跳）：`cd /mnt/nvme/calvin/code/deer-flow && set -a && . ./.env && set +a && uv run --project backend python benchmark/runner/upload_datasets.py --only routing`
- [ ] 全量重跑：`uv run --project backend python benchmark/runner/run_routing_experiment.py --run-name m4-behavior-v3b`
- [ ] 拉矩阵：`uv run --project backend python benchmark/runner/dump_run_scores.py --run-name m4-behavior-v3b`——
  读法：E2/E3/E6 收编+观测双修后应绿、A9 挂 —、A1–C2 首次带修复跑；B2 看答复摘要裁决
  （orchestrate 前门反问=改金标口径，真漏问=再治）；哪条「跑挂了」直接看崩因行。

</details>

### ② 清单套定额对照表补全 —— 跑批已收官（2026-07-06），剩前端验收

用 LLM 按意思（而非名字长得像）给清单项配定额子目（`ce-code/cost/bill_quota_enrich.py`，
commit `dd1d9a78`：4 路并发 + 每 25 码增量落库）。三重防错：只在候选内挑 / 措施项（模板脚手架泵送费）
代码拦截 / 拿不准留空转人工。**跑批结果（419 码全处理）**：
- ✅ 覆盖率 **11.2% → 26.9%**（GB/T 50854-2024 房建：127/472 码会套定额了；本轮净增 74 码 / 229 条边）
- ✅ 新配的平均每码 3.1 条边 vs 老的名字包含匹配 12.9 条——更全也更收敛；诚实空 335、红线拦下 30、低置信 0
- ✅ 四靶人眼抽验过：矩形柱/独立基础核心边全对；三条过宽边（承台→独立基础等）不修——对照表定位是
  候选池，组价时 32B 按特征二次消歧 + 人工闸兜底
- 增量全部 `source='semantic_llm'`，可按 source 整批回滚
**剩余动作**：
- [x] **前端端到端验收通过（2026-07-07，task 94055ecb，M4 正式收官）**：dev 态走全链，「独立基础 120m³ +
  矩形柱」2024 口径 18 步到**总造价 819,487.24**——两件全定码、预制码陷阱系统自排除（010502006 现浇 vs
  010503001 预制，reason 明确写出排除理由）、低置信（0.144/0 < τ0.6）人工闸兜底、定额带出本体浇筑子目
  （010002-28/010002-34，非模板措施，对照表补全直接兑现）、审计 11 条可追溯。
  **排障记录**：首跑模型自由发挥（自选错码+编造工具名）——根因 dev 后端 `CE_ROUTE_CONTEXT_URL` 为空串
  中间件未注册（launch.json envFile 解析问题），显式 env 块写死后链路即通；空串=不注册与 None 同效（agent.py
  `if route_ctx_url:`），before/after 对照是第零跳收权价值的绝佳素材。
- [x] **端到端对账（算术链）**：合价/分部分项/税前/税金/总造价逐级手算复核**零误差**（`pricing.py` 纯函数
  验证）。市场合理性对账（费率/措施费本次为测试值 1）留待真实费率输入或造价专家基准——归真实流量阶段。
- [ ] 下轮补跑：50856 安装册（`--spec-version "GB/T 50856-2024"` 定向）+ 本轮 10 条 LLM 失败码
  （**`--codes` 精确定向参数已加**，2026-07-06；失败码从跑批日志 `LLM 失败跳过` 行抓。别整轮重跑，335 条诚实空会被浪费重判）

---

## 3. M4 之后主线（按建议顺序）

1. **数据线收尾**：对照表补全只是第一步。剩余靠采购/人工：SJG 缺分册、信息价月刊、2013 定额源 PDF——
   决定默认口径（深圳·2013）何时从「诚实 501」变全链可用（`SPEC_REGISTRY` 翻 `supports_compose=True`，任务层零改码）。
2. **真实流量闭环**（价值密度最高）：清 `ce-services/TODO.md` 内网试用 checklist 余项 → 起量 → 难例回灌。
   直接解锁：金标扩 30+ → τ=0.25 复核（分流 20%→更高）、8b/32b 选码决策、Critic 金标 C4。
3. **评测深化**（有真流量后更值）：L1 路由金标从零合成（每落点 ≥50、总量 800~1200；现 34 例仅回归护栏）、
   L6-C 规范问答忠实度（RAGAS faithfulness ≥0.9、引用准确率 100%）、L3 检索评测（Recall@k/MRR + 版本对齐 + 地域泄漏=0）、
   **L7 NFR 压测（P95 延迟是 PRD 硬指标但从未量过**：FR-K ≤3s / FR-P01 ≤2s / ReAct 单轮 ≤5s）、
   L6-A 组价终态 pass^5（依赖 2013 数据）、L5 复合拆解 P/R/F1。

---

## 4. 小尾巴（不阻塞主线，择机清）

- [ ] CI 两个红的日志一直没拿到：lint-frontend（疑 pnpm frozen-lockfile）+ backend Unit Tests——`gh run view --log-failed` 定位，机械修
- [ ] config.yaml 版本 9→10 升级 warning（`make config-upgrade`）
- [ ] 前端三条人工判读（B1 口径声明行 / A1→A9 会话粘性 / B11 出界话术转述）
- [ ] quantity-drop 静默（可观测性补出声）；8B 收尾话术
- [ ] `cv.md`（根目录未入库）：面试材料——τ 调参有据不动 / 多智能体证伪 / 单轮口径冤判归因 / 架构对账七步表，都是好故事
- [ ] UI 抛光（note 已改，先验效果）；`ce-services/TODO.md` 内网试用 checklist 对账勾销

## 5. 挂起（决策在案，勿重复排查）

- **数据采购**（用户定：放最后）：SJG 缺分册 / 信息价月刊 / 真实设计说明（金标扩容原料）
- **τ=0.25 下探**：待金标 30+ 复核（[0.2,1] 桶 4/4 依据在案，`docker/ce-services/docker-compose.yaml` 注释有全程归因）
- **B2 原生「停-答-续」HITL**：代价大一个量级，仅当「agent 必须在场」被证实刚需才启动，前置调研 0.5–1 人日
- **联网兜底重开**：`CE_NORM_WEB_FALLBACK=0` 保持；路线 A 代理 / B 国内搜索 API，见 `AGENT_DEV.md` §8.4
- **silver→gold 升级**：等造价专家（AGENT_BENCHMARK §8.4 硬边界）
- **多智能体选码/复核**：已证伪（P1=P2=P3=96%），不再投入；Critic 保持人审辅助定位
