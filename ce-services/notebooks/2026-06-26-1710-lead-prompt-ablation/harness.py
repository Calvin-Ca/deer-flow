#!/usr/bin/env python3
"""lead-agent 提示词消融 harness（problem 6 / problem 7 编排层 S1·S2）。

在**不改框架源码**的前提下，对同一套评测集逐条跑多个 system-prompt 变体，自动量
「路由率 / 红线遵守率 / web 兜底率 / 越界拒答率」四项编排层指标，产出逐变体指标表与
逐条 trace，供 PROBLEM.md 第 6 节「提示词优化」候选方案做 A/B 选型。

变体切换机制：复用**生产同款**配置开关——把 `get_app_config().lead_agent.system_prompt_path`
指向当前变体文件 + `DeerFlowClient.reset_agent()` 强制重建 agent。`apply_prompt_template()`
内的 `_resolve_system_prompt_template()` 会按该路径读模板（app_config 为 None 时回退全局
单例，故 DeerFlowClient 路径同样生效）。这条路径与正式环境改 config.yaml 切版本完全一致，
不再 monkeypatch 框架内部。三个变体的占位符集都是当前 `apply_prompt_template` 已在传的
kwargs 子集，故可被同一框架直接 format。

判定口径（基于 `DeerFlowClient.stream` 的 StreamEvent）：
- clarified  = 出现 name=="ask_clarification" 的 tool call
- routed     = 出现 name=="bash" 且命令含 qa.py / cost.py 的 tool call
- web_fallback = 出现 web_search / web_fetch / image_search 的 tool call（B 摘 web 后应恒 False，留作回归哨兵）

多轮：no_version 用例第一轮应反问，第二轮喂 gold 版本后再判是否调脚本（这是 problem 6
「拿到版本后退回 web」现象的命门），故 routed 对 no_version 取**第二轮**。

只跑得动在服务器（需 config.yaml 指向 Qwen3-8B + 四服务起齐）；本地仅 `py_compile` 自检。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

WEB_TOOLS = {"web_search", "web_fetch", "image_search"}
SCRIPT_MARKERS = ("qa.py", "cost.py")
# 走子 agent 工具（而非 bash 直跑脚本）触达本地 skill 的路径：V0 通用 prompt 的典型行为，
# 也是 problem 6「把 skill 当工具表里的工具」反模式。单列一项指标，不并入 route_rate（直跑）。
SUBAGENT_TOOLS = {"norm-qa", "cost-agent"}


def load_variants(prompts_dir: Path) -> dict[str, Path]:
    """加载 prompts/ 目录下的全部提示词变体。

    参数：
        prompts_dir: 存放 v*_*.txt 变体文件的目录。
    返回：
        {变体名(去扩展名): 变体文件路径}，按文件名排序，保证 v0/v1/v2 顺序稳定。
        返回路径而非原文：切变体走 config.lead_agent.system_prompt_path（生产同款），
        由框架自己读文件，harness 不再持有/注入模板文本。
    """
    variants: dict[str, Path] = {}
    for path in sorted(prompts_dir.glob("*.txt")):
        variants[path.stem] = path.resolve()
    if not variants:
        raise FileNotFoundError(f"未在 {prompts_dir} 找到任何 *.txt 提示词变体")
    return variants


def load_eval(path: Path) -> list[dict]:
    """读取 agent_routing_eval.jsonl 评测集。

    参数：
        path: jsonl 评测集路径（每行一个用例，字段见 ce-services/eval/README.md）。
    返回：
        用例 dict 列表。
    """
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _apply_variant(variant_path: Path, client) -> None:
    """把某个提示词变体经生产同款配置开关注入框架并重建 agent。

    参数：
        variant_path: 变体模板文件路径。
        client:       DeerFlowClient 实例，注入后调用其 reset_agent() 生效。
    返回：
        无（副作用：改写全局 AppConfig 的 lead_agent.system_prompt_path 并重建 agent）。
    """
    from deerflow.config import get_app_config

    # 与正式环境改 config.yaml 同一条路径：apply_prompt_template 的
    # _resolve_system_prompt_template 会按此 path 读模板（client 路径 app_config=None
    # 时回退到这个全局单例）。harness 单进程内不触发 config 重载，故就地改持续生效。
    get_app_config().lead_agent.system_prompt_path = str(variant_path)
    client.reset_agent()


def run_one(client, thread_id: str, message: str) -> dict:
    """跑单轮对话，**先聚合整轮全部 tool-call 的 name 与 args**，再归纳判定。

    为何先聚合：LangGraph 流式下同一个 tool call 的 `name` 和 `args` 会落在不同
    chunk —— 带 `name=="bash"` 的 chunk 其 args 常为空 `{}`，命令文本在随后
    `name==""` 的 chunk 才补齐。旧逻辑「在 name=="bash" 的同一 chunk 上查 args 含
    qa.py」永远匹配不到，导致 route 判定恒 False。改为把整轮所有 args 拼成一个 blob、
    所有非空 name 收成一串后统一判，规避 name/args 跨 chunk 分裂。

    参数：
        client:    DeerFlowClient 实例。
        thread_id: 线程 id（同一用例多轮用同一个，靠 checkpointer 续上下文）。
        message:   本轮发给 agent 的用户文本。
    返回：
        {clarified, routed, routed_subagent, web_fallback, tool_calls:[name...], args_preview}
        - routed          直跑脚本：聚合 args 里出现 qa.py / cost.py（仅 bash 调脚本会带此路径）。
        - routed_subagent 走 norm-qa / cost-agent 子 agent 工具触达本地 skill（problem 6 反模式）。
    """
    tool_names: list[str] = []
    args_chunks: list[str] = []

    for ev in client.stream(message, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        data = ev.data or {}
        if data.get("type") != "ai":
            continue
        for tc in data.get("tool_calls", []) or []:
            name = tc.get("name", "")
            if name:
                tool_names.append(name)
            args = tc.get("args", {})
            if args:
                args_chunks.append(json.dumps(args, ensure_ascii=False))

    args_blob = " ".join(args_chunks)
    clarified = "ask_clarification" in tool_names
    # 直跑脚本：脚本路径（含 qa.py / cost.py）只可能出现在 bash 命令 args 里——
    # read_file 读的是 SKILL.md（路径不含 .py），故聚合 args 命中 marker 即真正发起了脚本调用。
    routed = any(m in args_blob for m in SCRIPT_MARKERS)
    routed_subagent = any(n in SUBAGENT_TOOLS for n in tool_names)
    web_fallback = any(n in WEB_TOOLS for n in tool_names)
    return {
        "clarified": clarified,
        "routed": routed,
        "routed_subagent": routed_subagent,
        "web_fallback": web_fallback,
        "tool_calls": tool_names,
        "args_preview": args_blob[:600],  # 留证供回看实际命令
    }


def eval_case(client, case: dict) -> dict:
    """跑单个评测用例（no_version 组跑两轮，其余一轮），归纳该用例的判定。

    参数：
        client: DeerFlowClient 实例（变体已注入）。
        case:   一条评测用例 dict。
    返回：
        在原 case 上补 {r1, r2, clarified, routed, routed_subagent, web_fallback} 的结果 dict。
        - clarified  取第一轮（红线在第一轮反问）。
        - routed / routed_subagent  no_version 取第二轮（拿到版本后是否调脚本/子 agent）；其余取第一轮。
        - web_fallback 任一轮出现即 True。
    """
    thread_id = f"ablate-{case['id']}-{uuid.uuid4().hex[:8]}"
    r1 = run_one(client, thread_id, case["query"])
    r2 = None
    routed = r1["routed"]
    routed_subagent = r1["routed_subagent"]
    if case.get("group") == "no_version" and case.get("gold"):
        # 第二轮喂澄清后的版本，模拟用户回答；problem 6 的命门在这一轮
        r2 = run_one(client, thread_id, str(case["gold"]))
        routed = r2["routed"]
        routed_subagent = r2["routed_subagent"]
    web_fallback = r1["web_fallback"] or (bool(r2) and r2["web_fallback"])
    return {**case, "r1": r1, "r2": r2, "clarified": r1["clarified"],
            "routed": routed, "routed_subagent": routed_subagent, "web_fallback": web_fallback}


def compute_metrics(results: list[dict]) -> dict:
    """按 ce-services/eval/README.md 口径汇总四项编排层指标。

    参数：
        results: eval_case 产出的逐条结果列表。
    返回：
        {route_rate, redline_rate, web_fallback_rate, boundary_reject_rate, n_*} 指标字典。
    """
    R = [r for r in results if r.get("expect_route")]
    C = [r for r in results if r.get("group") == "no_version"]
    B = [r for r in results if r.get("group") == "boundary"]

    def rate(num, den):
        return round(num / den, 4) if den else None

    return {
        "route_rate": rate(sum(r["routed"] for r in R), len(R)),
        # 直跑(routed)或走子 agent(routed_subagent)任一触达本地 skill 即算「未退 web」，
        # 该项与 route_rate 的差就是 V0 那种「把 skill 当不透明子 agent 工具」的占比。
        "subagent_route_rate": rate(sum(r["routed_subagent"] for r in R), len(R)),
        "reach_skill_rate": rate(sum(r["routed"] or r["routed_subagent"] for r in R), len(R)),
        "redline_rate": rate(sum(r["clarified"] for r in C), len(C)),
        "web_fallback_rate": rate(sum(r["web_fallback"] for r in results), len(results)),
        # 越界须既不直跑也不走子 agent（两条触达 skill 的路都不能走）
        "boundary_reject_rate": rate(sum(not (r["routed"] or r["routed_subagent"]) for r in B), len(B)),
        "n_total": len(results), "n_route": len(R), "n_no_version": len(C), "n_boundary": len(B),
    }


def render_metrics_md(per_variant: dict[str, dict]) -> str:
    """把多变体指标渲染成对比 Markdown 表（供 README「结果」段直接贴）。

    参数：
        per_variant: {变体名: 指标字典}。
    返回：
        Markdown 字符串。
    """
    lines = [
        "| 变体 | 路由率(直跑) | 子agent路由率 | 触达skill率 | 红线遵守率(主) | web兜底率(应0) | 越界拒答率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, m in per_variant.items():
        lines.append(
            f"| {name} | {m['route_rate']} | {m['subagent_route_rate']} | {m['reach_skill_rate']} | "
            f"{m['redline_rate']} | {m['web_fallback_rate']} | {m['boundary_reject_rate']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    """harness 入口：逐变体注入、跑评测集、落指标表与逐条 trace。

    参数：
        无（从命令行读 --eval/--prompts-dir/--out/--model）。
    返回：
        进程退出码（0 成功）。
    """
    ap = argparse.ArgumentParser(description="lead-agent 提示词消融 harness")
    here = Path(__file__).resolve().parent
    ap.add_argument("--eval", type=Path, default=here.parents[1] / "eval" / "agent_routing_eval.jsonl")
    ap.add_argument("--prompts-dir", type=Path, default=here / "prompts")
    ap.add_argument("--out", type=Path, default=here / "results")
    ap.add_argument("--model", type=str, default=None, help="config.yaml 里的模型名；缺省用默认模型")
    args = ap.parse_args()

    from deerflow.client import DeerFlowClient

    try:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()
    except Exception:  # 不同版本路径兜底
        from langgraph.checkpoint.memory import MemorySaver as InMemorySaver  # type: ignore
        checkpointer = InMemorySaver()

    variants = load_variants(args.prompts_dir)
    cases = load_eval(args.eval)
    args.out.mkdir(parents=True, exist_ok=True)

    client = DeerFlowClient(model_name=args.model, checkpointer=checkpointer)
    per_variant: dict[str, dict] = {}
    raw_path = args.out / "raw_traces.jsonl"
    with raw_path.open("w", encoding="utf-8") as raw_f:
        for vname, variant_path in variants.items():
            print(f"\n===== 变体 {vname} =====", flush=True)
            _apply_variant(variant_path, client)
            results = []
            for case in cases:
                res = eval_case(client, case)
                results.append(res)
                raw_f.write(json.dumps({"variant": vname, **res}, ensure_ascii=False) + "\n")
                tag = "route" if res["routed"] else "noroute"
                clar = "clarify" if res["clarified"] else "-"
                print(f"  [{case['id']}/{case.get('group')}] {tag} {clar} web={res['web_fallback']}", flush=True)
            per_variant[vname] = compute_metrics(results)
            print(f"  → {per_variant[vname]}", flush=True)

    metrics_md = render_metrics_md(per_variant)
    (args.out / "metrics.md").write_text(metrics_md, encoding="utf-8")
    (args.out / "metrics.json").write_text(json.dumps(per_variant, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + metrics_md)
    print(f"逐条 trace → {raw_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
