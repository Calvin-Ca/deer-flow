"""任务2b：路由评测——逐条跑 agent、读回 trace、挂分到 Langfuse Dataset Run。

对 ``user-requests-routing``（缺省，路由主池）等数据集逐条把 query 喂给默认 lead agent（skill-only），
从 agent 的工具调用里程序化判定两项（对标 L1_routing/README 的两率）：
    - clarify_correct：是否「该反问就反问」（命中 ask_clarification 工具）；
    - route_correct ：是否「该调脚本就调」（工具调用/参数命中 ROUTE_SIGNALS）。
每条跑完读回其 Langfuse trace，调 ``dataset_run_items.create`` 关联进同名 dataset
run（UI 里 Datasets→Runs 可横向比 prompt variant），并 ``create_score`` 挂两项分。

为什么不用 ``dataset.run_experiment``：它在自己的事件循环线程里同步调 task，而
``DeerFlowClient.stream`` 内部自驱 async + 持久 MCP（streamable_http）会话，二者
生命周期一打架就崩（cancel scope / Task destroyed）。本脚本改为**主线程、逐条、
不另起 loop**，与冒烟测试完全同构——那条路已验证干净退出。

判定信号是「外部观测启发式」：路由是否发生靠匹配 agent 实际调用的工具名是否在
ROUTE_TOOL_NAMES 里（统一精确名，不用前缀）。跑一轮后照真实 trace 里 agent 的实际
工具名回校本常量。

运行（服务器上，需四服务起齐使 agent 真能调脚本）：
    uv run --project backend python benchmark/L1_routing/run_routing_experiment.py \
        --run-name v2_runbook --model qwen-plus
退出码 0=完成。两率聚合见终端 + Langfuse UI 的 dataset run。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))  # _lf / _paths

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path（import app 前置）
from _lf import require_langfuse, wait_for_traces  # noqa: E402

DATASET_NAME = "user-requests-routing"  # 路由主池（78 条，仅深圳·2013 口径；原冻结集 agent-routing-eval 已审并入、停用）

# 「发生了路由」= lead agent 调了正经的算量/路由工具（按工具名判定，可靠：名字在流式
# tool_call 首片里就到齐，不像 args 会分片）。口径见 L1_routing/README：路由 = 调
# cost_workflow_* 工作流节点或 task 分派子智能体；bash/read_file 自己瞎折腾不算。
# 集合由 config.yaml + benchmark/prompts/lead_agent_v*.md 的「lead 可见工具面」确定（统一精确名、不用前缀）：
#   · cost 路由 = cost_workflow_start/node/resume/state（group=cost，lead 可见）
#   · norm 路由 = task 分派 norm-qa（顶层无 norm 编排工具，委派入口是 task）；**外加**当前 tool_search
#     关闭态下 lead 直调 ce-rag_search_clause 检索规范（见下方 2026-07-13 校正），亦计 norm 路由达成
#   · 单点能力直调 = bill_match（选码⇄核实双模，2026-07-12 由 verify_bill_code 合并更名）/
#     quota_recommend（定额方案推荐，2026-07-12 引擎化、原 quota-recommend 子智能体退役）/
#     price_query（信息价/走势，2026-07-12 引擎化提升为 lead 直调）/
#     cost_calc（单点计算）——lead_agent_v2
#     的直调工具面（v1 提示词不引用但工具全局可见，调了同样算路由）
# 2026-07-13 校正：原判据「刻意不收 ce-rag_*」的理由是它被 DeferredToolFilterMiddleware 对 lead 隐藏——
#   但该中间件仅在 config.yaml `tool_search.enabled=true` 时才入链（见 lead_agent/agent.py 的 if 分支）；
#   当前 tool_search.enabled=false → 中间件未启用 → ce-rag_* 原样绑给 lead、lead 直接调（F5 实测 tools
#   含 ce-rag_search_clause）。norm 侧无引擎化直调工具（不同于 cost/price 已有 bill_match/price_query），
#   故直调 ce-rag_search_clause 就是 lead 唯一的 norm 直达路径 → 收进本集：expect_route=True（A 簇规范
#   问答）算路由达成、expect_route=False（域外，如 B11）算违规取数，两侧对称成立。待「摘 ce-rag 出 lead +
#   norm-qa 子 agent 委派」架构落地后此条应收回，改由 task+subagent_type=norm 判 norm 路由。集合待按真实
#   trace 校准（§3.3-1）：目前只见 ce-rag_search_clause，如冒出其他 norm 检索原语再补。
# 仍不收：① ce-db_* 结构化真值原语（cost/price 已由 bill_match/price_query 引擎化直调覆盖，lead 不直调裸原语）；
#   ② verify_norm 是引用忠实度回查、③ verify_cost 是复核内部辅助——均非路由入口。
# task 只表示「分派了某子智能体」，光看名字分不清路由到 cost 还是 norm（要区分得读 subagent_type）。
ROUTE_TOOL_NAMES = {
    "cost_workflow_start",
    "cost_workflow_node",
    "cost_workflow_resume",
    "cost_workflow_state",
    "bill_match",
    "quota_recommend",
    "price_query",
    "cost_calc",
    "task",
    "ce-rag_search_clause",  # 2026-07-13 收：tool_search 关闭态 lead 直调的 norm 规范检索（见上校正）
}
CLARIFY_TOOL = "ask_clarification"

# 「路由对不对」判据（§3.3-3）：金标 metadata.agent 属于**子智能体**落点的，检查 task 的 subagent_type 是否命中。
# 当前架构下只有 norm-qa 是清晰的子智能体路由落点（v3：规范问答 → task(norm-qa)，17 条 expect_route）；
# cost-agent/price 已引擎化为 lead 直调工具、cost-check 多走 cost_calc(check) 或定稿前复核——它们的
# 「对不对」归 toolcall 评测，不在此判。key 须与金标 agent 字段取值一致（见 user_requests.jsonl）。
AGENT_TO_SUBAGENT = {"norm-qa": "norm-qa"}


def _is_route_tool(name: str) -> bool:
    """工具名是否属于「正经路由工具」（精确名命中）。"""
    return name in ROUTE_TOOL_NAMES


def _is_fetch_tool(name: str) -> bool:
    """「真取数」工具——expect_route=False 的违规口径。"""
    return _is_route_tool(name)


def _drive_agent(agent_client, query: str, thread_id: str) -> dict:
    """把一条 query 喂给默认 lead agent，收集其工具调用与最终回复（主线程同步）。

    功能：评测的核心动作——只观测，不改 agent 行为；与冒烟测试同一调用路径。
    参数：agent_client 整轮复用的 DeerFlowClient（每条新建会让上一条的持久 MCP 会话在
        下一条的事件循环里被 GC 收尾 → anyio cancel scope 跨 task 的 RuntimeError 噪音）；
        query 用户问法；thread_id 本条会话 id（即 langfuse session_id，跑完据此读回 trace）。
    返回：dict —— answer 最终文本、tool_names 工具名列表、did_clarify/did_route 两判定。
    """
    tool_names: list[str] = []  # 只收非空工具名（流式后续分片 name 为空，跳过）
    subagent_types: list[str] = []  # task 调用的 subagent_type（判「路由到对的子智能体」§3.3-3）
    answer_parts: list[str] = []

    for ev in agent_client.stream(query, thread_id=thread_id):
        if ev.type == "values":
            # values 快照带**完整** tool_calls args；messages 流式分片只到 task 的 name、抓不到
            # subagent_type（实测 工具=['task'] 但 subagent_ok=False），从这里补齐（§3.3-3）。
            for m in ev.data.get("messages", []) or []:
                if m.get("type") == "ai":
                    for tc in m.get("tool_calls", []) or []:
                        if tc.get("name") == "task":
                            st = (tc.get("args") or {}).get("subagent_type")
                            if st:
                                subagent_types.append(st)
            continue
        if ev.type != "messages-tuple":
            continue
        d = ev.data
        if d.get("type") == "ai":
            for tc in d.get("tool_calls", []) or []:
                name = tc.get("name") or ""
                if name:
                    tool_names.append(name)
                if name == "task":  # 收 task 的 subagent_type：判委派到 norm 还是别处（§3.3-3）
                    st = (tc.get("args") or {}).get("subagent_type")
                    if st:
                        subagent_types.append(st)
            if isinstance(d.get("content"), str):
                answer_parts.append(d["content"])
        elif d.get("type") == "tool" and d.get("name"):
            # 工具结果侧也计名（去重保序兜掉正常路径的重影）：保险口径——凡**中间件代发**的
            # 工具调用（不经模型流式 tool_calls、只有 ToolMessage 可观测）都靠这行兜住。
            # 历史动机是哑火收编（after_model 把纯文本反问转 ask_clarification，E6 冤案），该机制
            # 随 RouteContextMiddleware 删除已不存在（3691cbd4，其实从未接线）；保留此行作保险。
            tool_names.append(d["name"])

    tool_names = list(dict.fromkeys(tool_names))  # 去重保序（tool_call 与其结果各计一次的重影）
    return {
        "answer": "".join(answer_parts)[:500],
        "tool_names": tool_names,
        "did_clarify": CLARIFY_TOOL in tool_names,
        "did_route": any(_is_route_tool(n) for n in tool_names),
        "did_fetch": any(_is_fetch_tool(n) for n in tool_names),
        "subagent_types": list(dict.fromkeys(subagent_types)),
    }


def _match(expected: object, actual: bool) -> bool | None:
    """期望值与实际行为是否一致；期望缺失时返回 None（该项不计分）。"""
    if expected is None:
        return None
    return bool(expected) == actual


def main() -> int:
    """逐条跑数据集、挂分、聚合两率。

    功能：见模块 docstring。
    参数：无（命令行 --run-name / --model）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="路由评测：逐条跑 agent 并挂分到 Langfuse Dataset Run")
    parser.add_argument("--run-name", default=None, help="dataset run 名（建议填 prompt variant，便于横向比）")
    parser.add_argument("--model", default=None, help="覆盖模型名，如 qwen-plus")
    parser.add_argument("--dataset", default=DATASET_NAME, help=f"Langfuse dataset 名（缺省 {DATASET_NAME} 主池；清单匹配专项集用 bill-match-routing）")
    args = parser.parse_args()

    client = require_langfuse()
    run_name = args.run_name or f"routing-{uuid.uuid4().hex[:8]}"
    dataset = client.get_dataset(args.dataset)
    # thread_id 掺进程级随机后缀：checkpointer 持久化（跨进程存活），--run-name 重名时若
    # thread_id 相同会静默**续跑上一轮的旧对话**（实锤：E7 首工具是 cost_workflow_resume，
    # 背着上轮 47k 历史开局直接 60k 撞 32k 上限）。后缀保证每次进程都是全新 thread。
    nonce = uuid.uuid4().hex[:6]

    # 整轮共用一个客户端：agent/MCP 会话只建一次，会话收尾只发生在进程退出（见 _drive_agent 注释）。
    from deerflow.client import DeerFlowClient

    # subagent_enabled 必须显式开：DeerFlowClient 默认 False → task 不绑定 → norm 路由全线
    # 假阴性（8B 会幻觉调用 'norm-qa' 工具名 / tool_search 摸 ce-rag 直调——F11 实锤）。
    # 与 debug.py 的同款修复对齐（CLAUDE.md §3.3「缺了 task 不绑定、子智能体路由假阴性」）。
    agent_client = DeerFlowClient(model_name=args.model, subagent_enabled=True)

    rows: list[dict] = []
    for i, item in enumerate(dataset.items):
        query = (item.input or {}).get("query", "")
        thread_id = f"exp-{run_name}-{nonce}-{item.id}"
        try:
            out = _drive_agent(agent_client, query, thread_id)
        except Exception as exc:  # noqa: BLE001 —— 单条崩不拖垮整轮（v3 实测一条异常废了后续 22 条）
            print(f"[{i + 1}/{len(dataset.items)}] {item.id} 跑挂了，跳过（不挂分）：{type(exc).__name__}: {exc}")
            continue

        exp = item.expected_output or {}
        # expect_route=False（出界/域外）的违规口径按「真取数」判：前门 orchestrate 合规（见 _is_fetch_tool）
        actual_route = out["did_route"] if exp.get("expect_route") else out["did_fetch"]
        route_ok = _match(exp.get("expect_route"), actual_route)
        clarify_ok = _match(exp.get("expect_clarify"), out["did_clarify"])
        # 单轮口径修正（m4-behavior-v1 归因定案）：金标「先反问、答后再路由」的用例
        # （expect_clarify 与 expect_route 同真），agent 正确止步于反问等人时，路由
        # 「未到环节」——原口径记 ✗ 是冤判（v1 里 B2/E1/E2/E5/E6 全属此类），改不计分。
        if exp.get("expect_clarify") and exp.get("expect_route") and out["did_clarify"] and not out["did_route"]:
            route_ok = None

        # 「路由对不对」（§3.3-3）：仅金标期望路由到子智能体的用例才判——task 的 subagent_type 是否命中。
        gold_agent = (item.metadata or {}).get("agent")
        expected_sub = AGENT_TO_SUBAGENT.get(gold_agent) if exp.get("expect_route") else None
        subagent_ok = (expected_sub in out["subagent_types"]) if expected_sub else None
        if subagent_ok is not None and route_ok is None:  # 正确止步于反问（路由未到环节）→ 同 route_ok，不判委派对不对
            subagent_ok = None

        # 读回本条 trace，关联进 dataset run 并挂分
        traces = wait_for_traces(client, session_id=thread_id, expected=1)
        trace_id = getattr(traces[0], "id", None) if traces else None
        if trace_id:
            client.api.dataset_run_items.create(run_name=run_name, dataset_item_id=item.id, trace_id=trace_id)
            if route_ok is not None:
                client.create_score(name="route_correct", value=1.0 if route_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"期望调脚本={bool(exp.get('expect_route'))} 实际={actual_route} 工具={out['tool_names']}")
            if clarify_ok is not None:
                client.create_score(name="clarify_correct", value=1.0 if clarify_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"期望反问={bool(exp.get('expect_clarify'))} 实际={out['did_clarify']}")
            if subagent_ok is not None:
                client.create_score(name="subagent_route_correct", value=1.0 if subagent_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"期望子智能体={expected_sub} 实际 subagent_type={out['subagent_types']} 工具={out['tool_names']}")

        rows.append({"id": item.id, "group": (item.metadata or {}).get("group"), "exp": exp, "out": out, "route_ok": route_ok, "clarify_ok": clarify_ok, "subagent_ok": subagent_ok, "trace_id": trace_id})
        print(f"[{i + 1}/{len(dataset.items)}] {item.id} route_ok={route_ok} clarify_ok={clarify_ok} subagent_ok={subagent_ok} 工具={out['tool_names']}")

    client.flush()

    # 聚合两率（口径见 L1_routing/README）：
    # 路由率 = expect_route=True 且到达路由环节的用例里真去调脚本的比例
    # （route_ok=None 的「正确止步于反问」条目不入分母，单轮口径修正见上）
    route_set = [r for r in rows if r["exp"].get("expect_route") is True and r["route_ok"] is not None]
    clarify_set = [r for r in rows if r["exp"].get("expect_clarify") is True]
    n_halted = sum(1 for r in rows if r["exp"].get("expect_route") is True and r["route_ok"] is None)
    route_rate = sum(r["out"]["did_route"] for r in route_set) / len(route_set) if route_set else float("nan")
    clarify_rate = sum(r["out"]["did_clarify"] for r in clarify_set) / len(clarify_set) if clarify_set else float("nan")
    # 「路由对不对」子率（§3.3-3）：仅金标落点为子智能体（norm-qa）的用例，subagent_type 命中比例
    subagent_set = [r for r in rows if r.get("subagent_ok") is not None]
    subagent_rate = sum(1 for r in subagent_set if r["subagent_ok"]) / len(subagent_set) if subagent_set else float("nan")

    print("\n========== 聚合 ==========")
    print(f"run_name        = {run_name}   model = {args.model or '默认'}")
    print(f"路由率           = {route_rate:.2%}  ( {sum(r['out']['did_route'] for r in route_set)}/{len(route_set)} ，另 {n_halted} 条正确止步于反问不计，建议门 ≥0.8 )")
    print(f"红线遵守率(反问) = {clarify_rate:.2%}  ( {sum(r['out']['did_clarify'] for r in clarify_set)}/{len(clarify_set)} ，建议门 ≥0.95 )")
    print(f"路由对不对(norm) = {subagent_rate:.2%}  ( {sum(1 for r in subagent_set if r['subagent_ok'])}/{len(subagent_set)} ，期望 task(norm-qa) 命中率 )")
    print(f"逐条分数已挂到 Langfuse：Datasets → {args.dataset} → Runs → " + run_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
