#!/usr/bin/env python3
"""规范知识问答（code-qa）—— deer-flow skill 客户端（纯标准库 HTTP）。

设计：本脚本只是一个**薄 HTTP 客户端**，把查询转发给常驻的检索服务
（ce-code/service/server.py）。沙箱内**零第三方依赖**——只用
Python 标准库 urllib，无需 venv、无需向量索引数据、无需 POC 脚本。

用法（通过 deer-flow agent 的 bash 工具）：
    python3 qa.py --query "防火墙耐火极限要求"
    python3 qa.py --query "..." --top-k 20 --output /tmp/result.json

服务地址默认 http://localhost:8100，可用环境变量 CODE_QA_URL 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SERVICE_URL = os.environ.get("CODE_QA_URL", "http://localhost:8100")


def _post(url: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fail(obj: dict) -> None:
    """以 JSON 形式输出错误并以非零码退出（便于 agent 识别失败）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="建筑规范条文检索（HTTP 客户端）")
    parser.add_argument("--query", "-q", required=True, help="自然语言查询")
    parser.add_argument("--standard", default="gb50016", help="规范代号（默认 gb50016）")
    parser.add_argument("--top-k", type=int, default=20, help="最终返回条款数（强条不截断）")
    parser.add_argument("--skip-rerank", action="store_true", help="跳过 Rerank，用 RRF 排序")
    parser.add_argument(
        "--no-generate", action="store_true",
        help="只检索、不生成回答：打 /search 拿裸条款（给算量/审图等场景复用）",
    )
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="检索服务地址")
    parser.add_argument("--timeout", type=int, default=300, help="HTTP 超时（秒）")
    parser.add_argument("--output", default=None, help="结果写入 JSON 文件；不指定则输出到 stdout")
    args = parser.parse_args()

    # 默认 /qa（检索 + 结构化生成）；--no-generate 走 /search（裸条款）
    endpoint = "/search" if args.no_generate else "/qa"
    url = args.service_url.rstrip("/") + endpoint
    payload = {
        "query": args.query,
        "standard": args.standard,
        "top_k": args.top_k,
        "skip_rerank": args.skip_rerank,
    }

    try:
        result = _post(url, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        # 服务端用 HTTP 状态码 + detail 表达错误（如索引未就绪/检索失败）
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        _fail({
            "error": f"检索服务返回 {exc.code}",
            "detail": detail,
            "service_url": args.service_url,
        })
    except urllib.error.URLError as exc:
        _fail({
            "error": "无法连接检索服务",
            "detail": str(exc.reason),
            "service_url": args.service_url,
            "hint": "确认服务器上检索服务已启动：cd ce-code && .venv/bin/python service/server.py",
        })

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✓ 结果已写入 {args.output}", file=sys.stderr)
        # 同时把 meta 摘要打到 stderr，便于在前端步骤里一眼看到过程
        meta = result.get("meta", {})
        if meta:
            print(f"  meta: {json.dumps(meta, ensure_ascii=False)}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
