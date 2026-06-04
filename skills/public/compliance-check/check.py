#!/usr/bin/env python3
"""项目级合规检查 —— deer-flow skill 客户端（纯标准库 HTTP）。

设计：薄 HTTP 客户端，把项目描述转发给常驻合规服务
（ce-services/compliance/server.py 的 /compliance 端点）。沙箱内
**零第三方依赖**——只用标准库 urllib，无需 venv、向量索引或 POC 脚本。

用法（通过 deer-flow agent 的 bash 工具）：
    python3 check.py --project "地上11层住宅楼，总高32米，每层850平方米，地下一层车库"
    python3 check.py --project "..." --output /tmp/compliance_report.json

合规服务**独立部署在端口 8101**（与检索服务 :8100 分开）；可用环境变量
BUILDING_CODE_COMPLIANCE_URL 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SERVICE_URL = os.environ.get(
    "BUILDING_CODE_COMPLIANCE_URL", "http://localhost:8101"
)


def _post(url: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fail(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="项目级合规检查（HTTP 客户端）")
    parser.add_argument("--project", "-p", required=True, help="项目自由文本描述")
    parser.add_argument("--standard", default="gb50016", help="规范代号（默认 gb50016）")
    parser.add_argument("--skip-reflection", action="store_true", help="跳过反思校验（加速调试）")
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="合规服务地址")
    parser.add_argument("--timeout", type=int, default=600, help="HTTP 超时（秒，合规检查较慢）")
    parser.add_argument("--output", default=None, help="报告写入 JSON 文件；不指定则输出到 stdout")
    args = parser.parse_args()

    url = args.service_url.rstrip("/") + "/compliance"
    payload = {
        "project": args.project,
        "standard": args.standard,
        "skip_reflection": args.skip_reflection,
    }

    try:
        report = _post(url, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        _fail({
            "error": f"合规服务返回 {exc.code}",
            "detail": detail,
            "service_url": args.service_url,
        })
    except urllib.error.URLError as exc:
        _fail({
            "error": "无法连接合规服务",
            "detail": str(exc.reason),
            "service_url": args.service_url,
            "hint": "确认服务器上合规服务已启动（端口 8101）：cd ce-services && uv run python compliance/server.py",
        })

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✓ 报告已写入 {args.output}", file=sys.stderr)
        meta = report.get("meta", {})
        if meta:
            print(f"  meta: {json.dumps(meta, ensure_ascii=False)}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
