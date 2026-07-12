"""直接打 gateway 的单条路由探针（HTTP，不走嵌入式 runner）。

用途：给 debugpy 起的 gateway 发一条真实对话请求，用来①单步调试路由（断点在 gateway
进程里能命中）②肉眼看 lead agent 对某条 query 实际调了哪些工具、路由/反问对不对。
与评测 runner 不同——runner 是进程内嵌入式（DeerFlowClient），本脚本是**外部 HTTP 客户端**，
真正经过 gateway 的鉴权 / 中间件 / HTTP 那一层。

鉴权 & CSRF（gateway 要求）：登录（auth 端点走 Origin 校验、免 csrf token）→ 其响应
种下 `csrf_token` cookie（Double Submit）→ 取它做 `x-csrf-token` header 打非 auth 的 run POST。

凭据放 `.env`（已被 gitignore，安全）——在根 `.env` 加两行：
    DEER_FLOW_PROBE_EMAIL=you@example.com
    DEER_FLOW_PROBE_PASSWORD=xxx
之后命令即短（本脚本启动自动 load_dotenv 读它们）：
    uv run --project backend python benchmark/_shared/probe_gateway.py "C30现浇矩形柱怎么组价" --model qwen-plus
也可临时用 --email/--password 覆盖 .env。退出码 0=跑完；末尾打印工具名汇总 + 是否命中路由/反问。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from dotenv import load_dotenv

# 与 run_routing_experiment.py 同口径（lead 可见路由工具面）。
ROUTE_TOOL_NAMES = {"cost_workflow_start", "cost_workflow_node", "cost_workflow_resume", "cost_workflow_state", "bill_match", "quota_recommend", "price_query", "cost_calc", "task"}
CLARIFY_TOOL = "ask_clarification"


def _extract(obj, tool_names: list[str], texts: list[str]) -> None:
    """从任意 SSE data 结构里递归捞出工具名与 AI 文本。

    功能：gateway 的 messages 流每片结构不固定（dict/list 嵌套），统一深搜——凡遇
        `tool_calls` 列表收其 name、遇 tool 类消息收其 name、遇字符串 content 收进文本。
    参数：obj 解析后的 data；tool_names 收集工具名（原地追加）；texts 收集文本（原地追加）。
    返回：无（原地修改两个列表）。
    """
    if isinstance(obj, dict):
        tcs = obj.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                name = tc.get("name") if isinstance(tc, dict) else None
                if name:
                    tool_names.append(name)
        if obj.get("type") == "tool" and obj.get("name"):
            tool_names.append(obj["name"])
        content = obj.get("content")
        if isinstance(content, str) and content.strip() and obj.get("type") in (None, "ai", "AIMessageChunk", "AIMessage"):
            texts.append(content)
        for v in obj.values():
            _extract(v, tool_names, texts)
    elif isinstance(obj, list):
        for it in obj:
            _extract(it, tool_names, texts)


def main() -> int:
    """登录 gateway、流式跑一条 query、打印工具调用与路由/反问判定。

    功能：见模块 docstring。
    参数：无（命令行）。
    返回：进程退出码（0=成功）。
    """
    load_dotenv()  # 读根 .env 里的 DEER_FLOW_PROBE_EMAIL/PASSWORD（须在 argparse 取默认值前）
    parser = argparse.ArgumentParser(description="直接打 gateway 的单条路由探针")
    parser.add_argument("query", help="要发给 lead agent 的用户问法")
    parser.add_argument("--gateway", default=os.getenv("DEER_FLOW_PROBE_GATEWAY", "http://localhost:8001"), help="gateway 基址")
    parser.add_argument("--model", default=None, help="覆盖模型名，如 qwen-plus（默认走 config 默认模型）")
    parser.add_argument("--email", default=os.getenv("DEER_FLOW_PROBE_EMAIL"), help="登录邮箱（或 env DEER_FLOW_PROBE_EMAIL）")
    parser.add_argument("--password", default=os.getenv("DEER_FLOW_PROBE_PASSWORD"), help="登录密码（或 env DEER_FLOW_PROBE_PASSWORD）")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("缺登录凭据：设 --email/--password 或环境变量 DEER_FLOW_PROBE_EMAIL/DEER_FLOW_PROBE_PASSWORD", file=sys.stderr)
        return 2

    gw = args.gateway.rstrip("/")
    with httpx.Client(base_url=gw, timeout=120.0) as client:
        # ① 登录（auth 端点走 Origin 校验、不需 csrf token）——其响应会种下 session + csrf_token cookie
        r = client.post("/api/v1/auth/login/local", data={"username": args.email, "password": args.password})
        if r.status_code != 200:
            print(f"登录失败 {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return 1
        print(f"✓ 登录成功（{args.email}）")

        # ② 登录后 csrf_token cookie 已种下，取来做 Double Submit 的 header（非 auth 的 POST 必需）
        csrf = client.cookies.get("csrf_token")
        if not csrf:
            print("没拿到 csrf_token cookie（登录响应未种下？），run 会被 CSRF 拦。", file=sys.stderr)
            return 1
        headers = {"x-csrf-token": csrf}

        # ③ 流式跑一条 run
        payload = {
            "assistant_id": "lead_agent",
            "input": {"messages": [{"role": "user", "content": args.query}]},
            "stream_mode": ["messages", "values"],
        }
        if args.model:
            payload["context"] = {"model_name": args.model}

        tool_names: list[str] = []
        texts: list[str] = []
        print(f"→ 发起：{args.query}   model={args.model or '默认'}\n---- 流 ----")
        with client.stream("POST", "/api/runs/stream", json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")
                print(f"run 失败 {resp.status_code}: {body[:400]}", file=sys.stderr)
                return 1
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data in ("", "[DONE]"):
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                before = len(tool_names)
                _extract(obj, tool_names, texts)
                for n in tool_names[before:]:
                    print(f"  [tool] {n}")

    tools = list(dict.fromkeys(tool_names))  # 去重保序
    answer = "".join(texts)[-600:]
    did_route = any(n in ROUTE_TOOL_NAMES for n in tools)
    did_clarify = CLARIFY_TOOL in tools

    print("\n========== 汇总 ==========")
    print(f"工具调用（去重）= {tools}")
    print(f"命中路由工具 did_route   = {did_route}   （路由工具集 {sorted(ROUTE_TOOL_NAMES)}）")
    print(f"命中反问 did_clarify     = {did_clarify}")
    print(f"回复尾部：{answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
