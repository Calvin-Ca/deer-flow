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

判定信号是「外部观测启发式」：路由是否发生靠匹配 agent **第一次决策**里调用的工具名是否在
ROUTE_TOOL_NAMES 里（统一精确名，不用前缀）。跑一轮后照真实 trace 里 agent 的实际
工具名回校本常量。

**只测第一次工具决策**（2026-07-14 改）：路由对错在第一个带 tool_calls 的 AI 消息就定，
``_drive_agent`` 捕获它即 break、不执行工具、不往下跑。副产品：不再需要 ce-rag/ce-db 等工具
服务起齐（工具不执行）；也不会因后续多轮累积撑爆上下文（400）/打转撞递归——那些「路由已决
之后」的噪声整片消除。端到端跑通（工具真执行、整单闭环）属 L3/L7 的活，不在本路由基准里。

运行（服务器上；模型端点 :8099 需在，工具服务可不起）：
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
#   · norm 路由（2026-07-14 skill 化后）= lead 亲自做 agentic RAG：tool_search promote ce-rag（enabled=true 态）
#     / 直调 ce-rag_search_clause（enabled=false 态）/ 定稿 verify_norm 回查——三者任一即 norm 路由达成
#     （不再经 task 分派 norm-qa 子智能体；旧 v3/v4 的 task(norm-qa) 路径已随子智能体删除失效）
#   · 单点能力直调 = bill_match（选码⇄核实双模，2026-07-12 由 verify_bill_code 合并更名）/
#     quota_recommend（定额方案推荐，2026-07-12 引擎化、原 quota-recommend 子智能体退役）/
#     price_query（信息价/走势，2026-07-12 引擎化提升为 lead 直调）/
#     cost_calc（单点计算）——lead_agent_v2
#     的直调工具面（v1 提示词不引用但工具全局可见，调了同样算路由）
# 2026-07-14 校正（norm-qa skill 化）：norm 不再走 task 委派子智能体，改由 lead 亲自做 agentic RAG。
#   走 A 方案——ce-rag_* 保持 deferred（config `tool_search.enabled=true`），lead 用前经 tool_search promote：
#   · enabled=true（现值）→ 首个动作是 tool_search，随后调 ce-rag_*、定稿 verify_norm；
#   · enabled=false（ce-rag 直绑）→ 直调 ce-rag_search_clause；
#   三者（tool_search / ce-rag_search_clause / verify_norm）任一命中即 norm 路由达成，对两态都稳。
#   注：verify_norm 原被列为「非路由入口」，skill 化后它是每次 norm 定稿必调的强信号，故此番收进本集。
#   ⚠️ 集合仍待按真实 8B trace 校准（§4.1 F5→runner→trace）：若 8B 漏 promote 直接自答、或冒出其他
#   ce-rag 检索原语（get_clause/expand_clause_refs/retrieve_evidence），照实补进本集。
# 仍不收：① ce-db_* 结构化真值原语（cost/price 已由 bill_match/price_query 引擎化直调覆盖，lead 不直调裸原语）；
#   ② verify_cost 是 cost-critic 复核内部辅助——非路由入口。
# task 现只表示「分派 cost-critic 复核」（norm-qa 已 skill 化退出委派），仍收作路由信号。
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
    # norm 路由信号（2026-07-14 norm-qa skill 化后）：lead 亲自做 agentic RAG，两种运行态都收：
    #   · tool_search.enabled=true（config 现值，ce-rag deferred）→ 首个动作是 tool_search（promote ce-rag）
    #   · tool_search.enabled=false（ce-rag 直绑 lead）→ 直调 ce-rag_search_clause
    #   · verify_norm 为 skill 每次定稿必调的忠实性回查，亦作 norm 达成的强信号
    "tool_search",
    "ce-rag_search_clause",
    "verify_norm",
}
CLARIFY_TOOL = "ask_clarification"

# 「路由对不对」判据（§3.3-3）：金标 metadata.agent 属于**子智能体**落点的，检查 task 的 subagent_type 是否命中。
# 2026-07-14 norm-qa skill 化后，norm 不再经 task 委派、改由 lead 亲自做 agentic RAG（信号见 ROUTE_TOOL_NAMES）
# → 移出本表；现存子智能体只剩 cost-critic（定稿前复核），但它是 workflow 内部的复核落点、非本集金标路由靶。
# 故本表暂空，subagent_route_correct 子率对本集无适用用例（subagent_set 空 → nan，已在汇总处兜底）。
# cost-agent/price 已引擎化为 lead 直调工具、cost-check 多走 cost_calc(check)——它们的「对不对」归 toolcall 评测。
AGENT_TO_SUBAGENT: dict[str, str] = {}


def _is_route_tool(name: str) -> bool:
    """工具名是否属于「正经路由工具」（精确名命中）。"""
    return name in ROUTE_TOOL_NAMES


def _is_fetch_tool(name: str) -> bool:
    """「真取数」工具——expect_route=False 的违规口径。"""
    return _is_route_tool(name)


def _first_ai_message(messages: list[dict]) -> dict | None:
    """取消息列表里第一条 AI 消息（= agent 的第一次决策）；没有则 None。"""
    return next((m for m in messages if isinstance(m, dict) and m.get("type") == "ai"), None)


def _drive_agent(agent_client, query: str, thread_id: str) -> dict:
    """把一条 query 喂给默认 lead agent，**只观测第一次工具决策就收工**（主线程同步）。

    为什么只看第一次决策：路由对不对在 agent 的**第一个带 tool_calls 的 AI 消息**就定了；
    后续的工具执行、多轮往返与路由判定无关，却会 ① 累积上下文撑爆 32k（400）、② 打转撞递归、
    ③ 依赖 ce-rag/ce-db 起服务、④ 触发工具业务错误——全是「路由已决之后」的噪声。故一见到首个
    带 tool_calls 的 ``values`` 快照就 break：该快照在**工具节点执行之前**发出（本 thread 的 MCP
    会话尚未建立），break 干净、无跨 loop 关闭风险，也不需要工具服务起齐。

    只读 ``values`` 快照（`client.py` 每个状态快照都 emit 完整 messages，含 tool_calls 的完整
    args）——首个 AI 消息的 tool_calls 即路由决策，``task`` 的 ``subagent_type`` 也在其中（§3.3-3，
    不再受流式分片抓不全 args 的困扰）。首个 AI 消息若是纯文本（无 tool_calls）= agent 直接答复未
    路由（如 8B 在边界问题上自答），如实记为「未路由」。

    参数：agent_client 整轮复用的 DeerFlowClient；query 用户问法；thread_id 本条会话 id
        （即 langfuse session_id，跑完据此读回 trace）。
    返回：dict —— answer 首个 AI 文本、tool_names 首次决策的工具名、did_clarify/did_route/did_fetch
        三判定（口径收敛到**第一次决策**）、subagent_types 首次 task 的委派目标。
    """
    tool_names: list[str] = []
    subagent_types: list[str] = []
    answer_parts: list[str] = []

    gen = agent_client.stream(query, thread_id=thread_id)
    try:
        for ev in gen:
            if ev.type != "values":
                continue
            first_ai = _first_ai_message(ev.data.get("messages", []) or [])
            if first_ai is None:
                continue  # 模型还没回复，继续等下一个快照
            # 拿到 agent 第一次决策：读其 tool_calls（路由决策）+ 文本，然后立刻收工。
            for tc in first_ai.get("tool_calls", []) or []:
                name = tc.get("name") or ""
                if name:
                    tool_names.append(name)
                if name == "task":  # task 的 subagent_type：判委派到 norm 还是别处（§3.3-3）
                    st = (tc.get("args") or {}).get("subagent_type")
                    if st:
                        subagent_types.append(st)
            content = first_ai.get("content")
            if isinstance(content, str) and content:
                answer_parts.append(content)
            break  # 第一次决策已捕获，不再往下跑（工具节点未执行 → 无累积/打转/服务依赖）
    finally:
        # 确定性关闭生成器（LangGraph 同步流按 GeneratorExit 收尾），不留悬挂。break 发生在
        # 工具执行前、本 thread 无 MCP 会话，清理不该触发跨 loop 关闭；万一清理抛异常也吞掉，
        # 不让它冒泡把本条误判成「跑挂了」。
        try:
            gen.close()
        except Exception:
            pass

    tool_names = list(dict.fromkeys(tool_names))  # 去重保序（并行 tool_calls 里的重名）
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
            print(f"[{i + 1}/{len(dataset.items)}] {item.id} 跑挂了，跳过（不挂分）：{type(exc).__name__}: {exc} query={query!r}")
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
        print(f"[{i + 1}/{len(dataset.items)}] {item.id} route_ok={route_ok} clarify_ok={clarify_ok} subagent_ok={subagent_ok} 工具={out['tool_names']} query={query!r}")

    client.flush()

    # 聚合两率（口径见 L1_routing/README）：
    # 路由率 = expect_route=True 且到达路由环节的用例里真去调脚本的比例
    # （route_ok=None 的「正确止步于反问」条目不入分母，单轮口径修正见上）
    route_set = [r for r in rows if r["exp"].get("expect_route") is True and r["route_ok"] is not None]
    clarify_set = [r for r in rows if r["exp"].get("expect_clarify") is True]
    n_halted = sum(1 for r in rows if r["exp"].get("expect_route") is True and r["route_ok"] is None)
    route_rate = sum(r["out"]["did_route"] for r in route_set) / len(route_set) if route_set else float("nan")
    clarify_rate = sum(r["out"]["did_clarify"] for r in clarify_set) / len(clarify_set) if clarify_set else float("nan")
    # 「路由对不对」子率（§3.3-3）：仅金标落点为子智能体的用例判 subagent_type 命中比例。
    # 2026-07-14 norm-qa skill 化后 AGENT_TO_SUBAGENT 空 → subagent_set 空 → nan（本集暂无子智能体路由靶）。
    subagent_set = [r for r in rows if r.get("subagent_ok") is not None]
    subagent_rate = sum(1 for r in subagent_set if r["subagent_ok"]) / len(subagent_set) if subagent_set else float("nan")

    print("\n========== 聚合 ==========")
    print(f"run_name        = {run_name}   model = {args.model or '默认'}")
    print(f"路由率           = {route_rate:.2%}  ( {sum(r['out']['did_route'] for r in route_set)}/{len(route_set)} ，另 {n_halted} 条正确止步于反问不计，建议门 ≥0.8 )")
    print(f"红线遵守率(反问) = {clarify_rate:.2%}  ( {sum(r['out']['did_clarify'] for r in clarify_set)}/{len(clarify_set)} ，建议门 ≥0.95 )")
    print(f"路由对不对(委派) = {subagent_rate:.2%}  ( {sum(1 for r in subagent_set if r['subagent_ok'])}/{len(subagent_set)} ，norm-qa 已 skill 化，本集暂无子智能体路由靶→nan 正常 )")
    print(f"逐条分数已挂到 Langfuse：Datasets → {args.dataset} → Runs → " + run_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
