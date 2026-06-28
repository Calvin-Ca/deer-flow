#!/usr/bin/env python3
"""算量计价 CostAgent（cost-agent）—— deer-flow skill 客户端（纯标准库 HTTP）。

设计：本脚本只是一个**薄 HTTP 客户端**，把请求转发给常驻任务服务 :8101。沙箱内**零第三方依赖**——
只用 Python 标准库 urllib，无需 venv、无需向量索引数据、无需 POC 脚本。

两种模式：
- **一次性组价** `compose`：打 /cost/compose（候选召回 → LLM 选码 → 组价取数），无人介入、一把出结果。
  适合「只想知道某构件清单码/取数」的简单查询。
- **可中断 HITL 组价** `start` / `resume` / `state`：打 /cost/session/*，把 13 步组价拆成可暂停的闸
  （编码 / 定额 / 信息价 / 费率 / 参数 / 总造价复核），每个需人介入的点停下来等用户决策。
  适合「要走完整组价流程、要人确认/录入、要可审计」的场景——agent 逐闸用 ``ask_clarification`` 收用户
  决策、再 ``resume`` 续跑（流程编排在服务端的图里，本脚本只转发）。

⚠️ ``--spec`` **必填、无默认**（compose/start）：清单计量国标 2013/2024 同 9 位码不同义，版本错了就串库；
调用前必须确认用户要按哪版国标组价。2013 组价数据未就绪，只出选码不出价。

⚠️ **不杜撰**：选码 need_review / 缺价 no_source / 费率税率等政策数，一律如实透传 / 由用户录入，不替补。

用法（通过 deer-flow agent 的 bash 工具，用系统 python3）：
    # 一次性
    python3 cost.py compose -d "C30现浇混凝土矩形柱" --spec 2024
    # HITL：起会话 → 拿到 task_id + 首个闸
    python3 cost.py start -d "C30现浇矩形柱" --spec 2024 --region 深圳
    # HITL：带用户决策续跑（decision 为 JSON 字符串）
    python3 cost.py resume --task <task_id> --decision '{"action":"manual_override","value":"010503001"}'
    python3 cost.py resume --task <task_id> --decision '{"value": 5.5}'
    # 读当前状态（不推进）
    python3 cost.py state --task <task_id>

地址默认任务服务 http://localhost:8101，可用环境变量 COST_AGENT_URL 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SERVICE_URL = os.environ.get("COST_AGENT_URL", "http://localhost:8101")

# 合法国标版本（与 ce-code config.SPEC_REGISTRY 一致）。2013/2024 同码不同义，错版串库。
VALID_SPECS = ("2013", "2024")


def _request(url: str, payload: dict | None, timeout: int) -> dict:
    """对 url 发请求（payload=None → GET，否则 POST JSON），解析 JSON 响应。

    参数：url —— 目标地址；payload —— 请求体 dict（None 则 GET）；timeout —— 超时秒数。
    返回：解析后的响应 dict。
    """
    if payload is None:
        req = urllib.request.Request(url)
    else:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fail(obj: dict) -> None:
    """以 JSON 形式输出错误并以非零码退出（便于 agent 识别失败）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    raise SystemExit(1)


def _call(url: str, payload: dict | None, base_url: str, timeout: int) -> dict:
    """带统一异常→错误 JSON 映射地发请求（HTTPError 透传 detail / URLError 提示起服务）。"""
    try:
        return _request(url, payload, timeout)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        _fail({"error": f"服务返回 {exc.code}", "detail": detail, "service_url": base_url})
    except urllib.error.URLError as exc:
        _fail({
            "error": "无法连接服务", "detail": str(exc.reason), "service_url": base_url,
            "hint": "确认服务器上任务服务已启动：cd ce-services && uv run python main.py",
        })
    return {}  # 不可达（_fail 已 raise），仅为类型完整


def _emit(result: dict, output: str | None, summary: str | None = None) -> None:
    """输出结果 JSON（stdout 或写文件）；summary 打 stderr 便于一眼看关键状态。"""
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✓ 结果已写入 {output}", file=sys.stderr)
    else:
        print(text)
    if summary:
        print(summary, file=sys.stderr)


def _check_spec(spec: str) -> None:
    """spec 版本红线：必须是已支持国标版本，错版即拒（防 2013/2024 串库）。"""
    if spec not in VALID_SPECS:
        _fail({
            "error": "未知国标版本", "detail": f"--spec={spec!r} 不在支持列表内",
            "valid_specs": list(VALID_SPECS),
            "hint": "清单计量国标 2013/2024 同码不同义，请向用户确认按哪版国标组价",
        })


def _session_view(result: dict) -> dict:
    """把会话响应精简成 agent 逐闸驱动所需视图（去掉冗长 events/items，聚焦 task_id/status/gate）。

    参数：result —— /cost/session/* 原始响应。
    返回：``{task_id, status, gate, rollup?}``——gate=当前 interrupt 闸 payload（status=awaiting_input 时非空）；
      done 时附 rollup 总造价 + 审计/override 计数。完整明细（events/审计链）如需见 --output 落原始 JSON。
    """
    view: dict = {
        "task_id": result.get("task_id"),
        "status": result.get("status"),
        "gate": result.get("interrupt"),
    }
    if result.get("status") == "done":
        view["rollup"] = result.get("rollup")
        view["audit_count"] = len(result.get("audit_log", []))
        view["override_count"] = len(result.get("overrides", []))
    return view


def cmd_compose(args: argparse.Namespace) -> None:
    """一次性组价：/cost/compose（选码 + 组价取数，无人介入）。"""
    _check_spec(args.spec)
    url = args.service_url.rstrip("/") + "/cost/compose"
    payload = {"description": args.description, "spec": args.spec,
               "region": args.region, "top_k": args.top_k}
    result = _call(url, payload, args.service_url, args.timeout)
    sel = result.get("selection", {})
    _emit(result, args.output,
          summary=f"  code={result.get('code')} need_review={sel.get('need_review')} "
                  f"price_status={result.get('price_status')}")


def cmd_start(args: argparse.Namespace) -> None:
    """起 HITL 会话：/cost/session/start → 跑到首个闸或 done，返回 task_id + 闸。"""
    _check_spec(args.spec)
    url = args.service_url.rstrip("/") + "/cost/session/start"
    payload = {"feature": args.description, "spec": args.spec, "region": args.region}
    result = _call(url, payload, args.service_url, args.timeout)
    view = _session_view(result)
    gate = view.get("gate") or {}
    _emit(view, args.output,
          summary=f"  task_id={view.get('task_id')} status={view.get('status')} "
                  f"gate={gate.get('gate_type')}:{gate.get('node')}")


def cmd_resume(args: argparse.Namespace) -> None:
    """带用户决策续跑：/cost/session/{task}/resume（decision 为 JSON 字符串）。"""
    try:
        decision = json.loads(args.decision)
    except json.JSONDecodeError as exc:
        _fail({"error": "decision 不是合法 JSON", "detail": str(exc),
               "hint": '示例：{"action":"approve"} / {"action":"manual_override","value":"010503001"} / {"value": 5.5}'})
    url = args.service_url.rstrip("/") + f"/cost/session/{args.task}/resume"
    result = _call(url, {"decision": decision}, args.service_url, args.timeout)
    view = _session_view(result)
    gate = view.get("gate") or {}
    _emit(view, args.output,
          summary=f"  task_id={view.get('task_id')} status={view.get('status')} "
                  f"gate={gate.get('gate_type')}:{gate.get('node')}")


def cmd_state(args: argparse.Namespace) -> None:
    """读会话当前持久化状态（不推进图）：GET /cost/session/{task}/state。"""
    url = args.service_url.rstrip("/") + f"/cost/session/{args.task}/state"
    result = _call(url, None, args.service_url, args.timeout)
    _emit(result, args.output, summary=f"  status={result.get('status')} next={result.get('next')}")


def main() -> None:
    """解析子命令 → 分发（compose / start / resume / state）。

    参数：无（从命令行读取）。返回：无（结果打 stdout 或写 --output；失败抛 SystemExit(1)）。
    """
    parser = argparse.ArgumentParser(description="CostAgent 组价客户端（一次性 compose / 可中断 HITL session）")
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="任务服务地址（:8101）")
        p.add_argument("--timeout", type=int, default=300, help="HTTP 超时（秒）")
        p.add_argument("--output", default=None, help="结果写入 JSON 文件；不指定则输出到 stdout")

    p_compose = sub.add_parser("compose", help="一次性组价（选码 + 取数，无人介入）")
    p_compose.add_argument("--description", "-d", required=True, help="构件/做法描述")
    p_compose.add_argument("--spec", required=True, help=f"国标版本（必填）：{', '.join(VALID_SPECS)}")
    p_compose.add_argument("--region", default="深圳", help="地区，默认深圳")
    p_compose.add_argument("--top-k", type=int, default=10, help="清单候选召回数（默认 10）")
    _add_common(p_compose)
    p_compose.set_defaults(func=cmd_compose)

    p_start = sub.add_parser("start", help="起 HITL 会话，跑到首个闸或 done")
    p_start.add_argument("--description", "-d", required=True, help="构件/做法描述")
    p_start.add_argument("--spec", required=True, help=f"国标版本（必填）：{', '.join(VALID_SPECS)}")
    p_start.add_argument("--region", default="深圳", help="地区，默认深圳")
    _add_common(p_start)
    p_start.set_defaults(func=cmd_start)

    p_resume = sub.add_parser("resume", help="带用户决策续跑会话到下个闸或 done")
    p_resume.add_argument("--task", required=True, help="会话 task_id（start 返回）")
    p_resume.add_argument("--decision", required=True,
                          help='闸决策 JSON：confirm 闸 {"action":"approve|select_alternative|manual_override","value"?} / '
                               'input 闸字段值 dict 如 {"value":5.5} 或 {"management_fee_rate":10,...} / review 闸 {"action":"approve"}')
    _add_common(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_state = sub.add_parser("state", help="读会话当前持久化状态（不推进）")
    p_state.add_argument("--task", required=True, help="会话 task_id")
    _add_common(p_state)
    p_state.set_defaults(func=cmd_state)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
