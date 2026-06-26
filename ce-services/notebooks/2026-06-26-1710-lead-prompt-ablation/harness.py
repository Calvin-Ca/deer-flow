#!/usr/bin/env python3
"""lead-agent 提示词消融 harness（problem 6 / problem 7 编排层 S1·S2）。

在**不改框架源码**的前提下，对同一套评测集逐条跑多个 system-prompt 变体，自动量
「路由率 / 红线遵守率 / web 兜底率 / 越界拒答率」四项编排层指标，产出逐变体指标表与
逐条 trace，供 PROBLEM.md 第 6 节「提示词优化」候选方案做 A/B 选型。

变体切换机制：monkeypatch `deerflow.agents.lead_agent.prompt.SYSTEM_PROMPT_TEMPLATE`
（模块全局，被 `apply_prompt_template()` 读取）+ `DeerFlowClient.reset_agent()` 强制
重建 agent。三个变体的占位符集都是当前 `apply_prompt_template` 已在传的 kwargs 子集，
故可被同一框架直接 format。

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


def load_variants(prompts_dir: Path) -> dict[str, str]:
    """加载 prompts/ 目录下的全部提示词变体。

    参数：
        prompts_dir: 存放 v*_*.txt 变体文件的目录。
    返回：
        {变体名(去扩展名): 模板原文字符串}，按文件名排序，保证 v0/v1/v2 顺序稳定。
    """
    variants: dict[str, str] = {}
    for path in sorted(prompts_dir.glob("*.txt")):
        variants[path.stem] = path.read_text(encoding="utf-8")
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


def _apply_variant(template: str, client) -> None:
    """把某个提示词变体注入框架并重建 agent。

    参数：
        template: 变体模板原文（含 {agent_name} 等占位符）。
        client:   DeerFlowClient 实例，注入后调用其 reset_agent() 生效。
    返回：
        无（副作用：改写 prompt 模块全局变量并清缓存）。
    """
    import deerflow.agents.lead_agent.prompt as prompt_mod

    prompt_mod.SYSTEM_PROMPT_TEMPLATE = template
    # skills 段有 lru_cache，变体即使不动 skills 也清一次确保干净
    prompt_mod._get_cached_skills_prompt_section.cache_clear()
    client.reset_agent()


def run_one(client, thread_id: str, message: str) -> dict:
    """跑单轮对话，解析这一轮 agent 发起的 tool call，归纳三个布尔判定。

    参数：
        client:    DeerFlowClient 实例。
        thread_id: 线程 id（同一用例多轮用同一个，靠 checkpointer 续上下文）。
        message:   本轮发给 agent 的用户文本。
    返回：
        {clarified, routed, web_fallback, tool_calls:[name...]} —— 本轮判定与原始 tool 名序列。
    """
    tool_names: list[str] = []
    clarified = routed = web_fallback = False

    for ev in client.stream(message, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        data = ev.data or {}
        if data.get("type") != "ai":
            continue
        for tc in data.get("tool_calls", []) or []:
            name = tc.get("name", "")
            tool_names.append(name)
            args_blob = json.dumps(tc.get("args", {}), ensure_ascii=False)
            if name == "ask_clarification":
                clarified = True
            elif name == "bash" and any(m in args_blob for m in SCRIPT_MARKERS):
                routed = True
            elif name in WEB_TOOLS:
                web_fallback = True
    return {"clarified": clarified, "routed": routed, "web_fallback": web_fallback, "tool_calls": tool_names}


def eval_case(client, case: dict) -> dict:
    """跑单个评测用例（no_version 组跑两轮，其余一轮），归纳该用例的判定。

    参数：
        client: DeerFlowClient 实例（变体已注入）。
        case:   一条评测用例 dict。
    返回：
        在原 case 上补 {r1, r2, clarified, routed, web_fallback} 的结果 dict。
        - clarified  取第一轮（红线在第一轮反问）。
        - routed     no_version 取第二轮（拿到版本后是否调脚本）；其余取第一轮。
        - web_fallback 任一轮出现即 True。
    """
    thread_id = f"ablate-{case['id']}-{uuid.uuid4().hex[:8]}"
    r1 = run_one(client, thread_id, case["query"])
    r2 = None
    routed = r1["routed"]
    if case.get("group") == "no_version" and case.get("gold"):
        # 第二轮喂澄清后的版本，模拟用户回答；problem 6 的命门在这一轮
        r2 = run_one(client, thread_id, str(case["gold"]))
        routed = r2["routed"]
    web_fallback = r1["web_fallback"] or (bool(r2) and r2["web_fallback"])
    return {**case, "r1": r1, "r2": r2, "clarified": r1["clarified"], "routed": routed, "web_fallback": web_fallback}


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
        "redline_rate": rate(sum(r["clarified"] for r in C), len(C)),
        "web_fallback_rate": rate(sum(r["web_fallback"] for r in results), len(results)),
        "boundary_reject_rate": rate(sum(not r["routed"] for r in B), len(B)),
        "n_total": len(results), "n_route": len(R), "n_no_version": len(C), "n_boundary": len(B),
    }


def render_metrics_md(per_variant: dict[str, dict]) -> str:
    """把多变体指标渲染成对比 Markdown 表（供 README「结果」段直接贴）。

    参数：
        per_variant: {变体名: 指标字典}。
    返回：
        Markdown 字符串。
    """
    lines = ["| 变体 | 路由率 | 红线遵守率(主) | web兜底率(应0) | 越界拒答率 |", "|---|---|---|---|---|"]
    for name, m in per_variant.items():
        lines.append(f"| {name} | {m['route_rate']} | {m['redline_rate']} | {m['web_fallback_rate']} | {m['boundary_reject_rate']} |")
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
        for vname, template in variants.items():
            print(f"\n===== 变体 {vname} =====", flush=True)
            _apply_variant(template, client)
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
