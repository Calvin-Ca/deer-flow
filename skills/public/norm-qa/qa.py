#!/usr/bin/env python3
"""造价规范问答（norm-qa）—— deer-flow skill 客户端（纯标准库 HTTP）。

设计：本脚本只是一个**薄 HTTP 客户端**，把问题转发给常驻服务。沙箱内**零第三方
依赖**——只用 Python 标准库 urllib，无需 venv、无需向量索引数据、无需 POC 脚本。

两条路径打不同服务（任务层 / 知识层已拆分）：
  - 默认（问答）→ 任务服务 :8101 的 /norm/qa（检索造价规范条文 + Qwen3 带引用作答）
  - --no-generate（裸条款）→ 知识服务 :8100 的 /search（给算量/审图等场景复用）

⚠️ ``--standard`` **必填、无默认**：造价计量计价规范 2013/2024 同 9 位码不同义，
版本错了就串库；调用前必须确认用户要查哪部哪版规范（见下表代号）。

用法（通过 deer-flow agent 的 bash 工具）：
    python3 qa.py --query "矩形柱按什么计量？" --standard gb50854-2024
    python3 qa.py -q "..." --standard gb50500-2024 --output /tmp/result.json
    python3 qa.py -q "..." --standard gb50854-2013 --no-generate   # 只检索裸条款

地址默认任务服务 http://localhost:8101、知识服务 http://localhost:8100，
可分别用环境变量 NORM_QA_URL / NORM_QA_KNOWLEDGE_URL 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SERVICE_URL = os.environ.get("NORM_QA_URL", "http://localhost:8101")
DEFAULT_KNOWLEDGE_URL = os.environ.get("NORM_QA_KNOWLEDGE_URL", "http://localhost:8100")

# 合法规范代号（与 ce-code config.STANDARD_ALIASES 的造价计量计价规范一致）。
VALID_STANDARDS = (
    "gb50500-2013", "gb50500-2024",   # 工程量清单计价规范/标准
    "gb50854-2013", "gb50854-2024",   # 房屋建筑与装饰工程工程量计算规范/标准
    "gb50856-2024",                   # 通用安装工程工程量计算标准
)


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
    parser = argparse.ArgumentParser(description="造价规范条文检索 + 带引用问答（HTTP 客户端）")
    parser.add_argument("--query", "-q", required=True, help="自然语言问题")
    parser.add_argument("--standard", required=True,
                        help=f"规范代号（必填，无默认）：{', '.join(VALID_STANDARDS)}")
    parser.add_argument("--top-k", type=int, default=15, help="检索召回条数（默认 15）")
    parser.add_argument("--skip-rerank", action="store_true", help="跳过 cross-encoder 精排（调试用）")
    parser.add_argument(
        "--no-generate", action="store_true",
        help="只检索、不生成：打知识服务 /search 拿裸条款（给算量/审图等场景复用）",
    )
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="任务服务地址（:8101）")
    parser.add_argument("--knowledge-url", default=DEFAULT_KNOWLEDGE_URL, help="知识服务地址（:8100，--no-generate 用）")
    parser.add_argument("--timeout", type=int, default=300, help="HTTP 超时（秒）")
    parser.add_argument("--output", default=None, help="结果写入 JSON 文件；不指定则输出到 stdout")
    args = parser.parse_args()

    # 版本红线：代号必须在已建索引的造价规范内，错版即拒（防 2013/2024 串库）。
    if args.standard not in VALID_STANDARDS:
        _fail({
            "error": "未知规范代号",
            "detail": f"--standard={args.standard!r} 不在支持列表内",
            "valid_standards": list(VALID_STANDARDS),
            "hint": "造价计量计价规范 2013/2024 同码不同义，请向用户确认要查哪部哪版规范",
        })

    # 默认打任务服务 /norm/qa（检索 + 生成）；--no-generate 打知识服务 /search（裸条款）
    if args.no_generate:
        base_url = args.knowledge_url
        url = base_url.rstrip("/") + "/search"
    else:
        base_url = args.service_url
        url = base_url.rstrip("/") + "/norm/qa"
    payload = {
        "query": args.query,
        "standard": args.standard,
        "top_k": args.top_k,
        "skip_rerank": args.skip_rerank,
    }

    try:
        result = _post(url, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        # 服务端用 HTTP 状态码 + detail 表达错误（如未知规范 400 / 索引未就绪 503）
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
        startup = (
            "cd ce-code && uv run python -m service.knowledge_api"
            if args.no_generate
            else "cd ce-services && uv run python main.py"
        )
        _fail({
            "error": "无法连接服务",
            "detail": str(exc.reason),
            "service_url": base_url,
            "hint": f"确认服务器上对应服务已启动：{startup}",
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
