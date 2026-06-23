#!/usr/bin/env python3
"""算量计价 CostAgent（cost-agent）—— deer-flow skill 客户端（纯标准库 HTTP）。

设计：本脚本只是一个**薄 HTTP 客户端**，把构件描述转发给常驻任务服务做
「候选召回 → LLM 选码 → 组价取数」。沙箱内**零第三方依赖**——只用 Python 标准库
urllib，无需 venv、无需向量索引数据、无需 POC 脚本。

打任务服务 :8101 的 /cost/compose（内部串 :8100 知识服务的 bill_match / price_compose
+ Qwen3 选码）。返回选码（带 need_review 红线）+ 组价取数（工料机含量 + 信息价单价）。

⚠️ ``--spec`` **必填、无默认**：清单计量国标 2013/2024 同 9 位码不同义，版本错了就串库；
调用前必须确认用户要按哪版国标组价（2013 / 2024）。2013 组价数据未就绪，只出选码不出价。

⚠️ **不算钱**：P1 只到「选码 + 组价取数」，不组装综合单价/含税造价（那是确定性公式，P2）。

用法（通过 deer-flow agent 的 bash 工具）：
    python3 cost.py --description "C30现浇混凝土矩形柱" --spec 2024
    python3 cost.py -d "MU10标准砖240厚实心砖墙M5水泥砂浆" --spec 2024 --region 深圳 --output /tmp/cost.json

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


def _post(url: str, payload: dict, timeout: int) -> dict:
    """POST JSON 到 url 并解析 JSON 响应。

    参数：url —— 目标地址；payload —— 请求体 dict；timeout —— 超时秒数。
    返回：解析后的响应 dict。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fail(obj: dict) -> None:
    """以 JSON 形式输出错误并以非零码退出（便于 agent 识别失败）。

    参数：obj —— 错误信息 dict（error/detail/hint 等）。
    返回：无（抛 SystemExit(1)）。
    """
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    raise SystemExit(1)


def main() -> None:
    """解析参数 → 校验 spec 红线 → 打 /cost/compose → 输出选码+组价结果。

    参数：无（从命令行读取）。
    返回：无（结果打到 stdout 或写入 --output 文件；失败抛 SystemExit(1)）。
    """
    parser = argparse.ArgumentParser(description="构件描述 → 选码 → 组价取数（HTTP 客户端）")
    parser.add_argument("--description", "-d", required=True, help="构件/做法自然语言描述")
    parser.add_argument("--spec", required=True,
                        help=f"国标版本（必填，无默认）：{', '.join(VALID_SPECS)}")
    parser.add_argument("--region", default="深圳", help="地区，默认深圳（用于信息价/定额取数）")
    parser.add_argument("--top-k", type=int, default=10, help="清单候选召回数（默认 10）")
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="任务服务地址（:8101）")
    parser.add_argument("--timeout", type=int, default=300, help="HTTP 超时（秒）")
    parser.add_argument("--output", default=None, help="结果写入 JSON 文件；不指定则输出到 stdout")
    args = parser.parse_args()

    # 版本红线：spec 必须是已支持的国标版本，错版即拒（防 2013/2024 串库）。
    if args.spec not in VALID_SPECS:
        _fail({
            "error": "未知国标版本",
            "detail": f"--spec={args.spec!r} 不在支持列表内",
            "valid_specs": list(VALID_SPECS),
            "hint": "清单计量国标 2013/2024 同码不同义，请向用户确认按哪版国标组价",
        })

    base_url = args.service_url
    url = base_url.rstrip("/") + "/cost/compose"
    payload = {
        "description": args.description,
        "spec": args.spec,
        "region": args.region,
        "top_k": args.top_k,
    }

    try:
        result = _post(url, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        # 服务端用 HTTP 状态码 + detail 表达错误（如未知 spec 400 / 知识服务 503 / 组价清单不存在 404）
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        _fail({
            "error": f"服务返回 {exc.code}",
            "detail": detail,
            "service_url": base_url,
        })
    except urllib.error.URLError as exc:
        _fail({
            "error": "无法连接服务",
            "detail": str(exc.reason),
            "service_url": base_url,
            "hint": "确认服务器上任务服务已启动：cd ce-services && uv run python main.py",
        })

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✓ 结果已写入 {args.output}", file=sys.stderr)
        # 把选码/红线摘要打到 stderr，便于在前端步骤里一眼看到关键状态
        sel = result.get("selection", {})
        print(f"  code={result.get('code')} need_review={sel.get('need_review')} "
              f"price_status={result.get('price_status')}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
