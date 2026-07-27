"""
vLLM 吞吐探针：定位「加了 --workers 却不提速」的瓶颈在客户端还是服务端。

背景：多次实测聚合输出吞吐恒为约 26 tok/s，与并发数无关——
  D1  386k tok / 4h14m  = 25.3 tok/s（--workers 8）
  D2  960k tok / 9h41m  = 27.5 tok/s（--workers 8）
  B smoke  40k tok / 25m45s = 25.9 tok/s（--workers 8）
且 B smoke 的总耗时（1545s）≈ 单条耗时 × 条数（30.8s × 50 = 1540s），
即 8 个 worker 完全没有重叠。本探针用受控实验区分两种可能：

  A. 客户端串行  —— llm.py 的 _RateLimiter 持锁 sleep，或连接池限制
  B. 服务端不批  —— vLLM 侧 max-num-seqs 过小 / 被其他负载占满

判据：并发从 1 升到 8，若**单请求延迟基本不变、总耗时接近不变**，说明服务端在批处理，
问题在客户端；若**单请求延迟随并发成倍增长**（总耗时不降），说明服务端在排队。

运行：
  python -m src.utils.probe_throughput                      # 探默认合成 endpoint
  python -m src.utils.probe_throughput --base-url http://localhost:8099/v1 --model qwen3-8b
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

_PROMPT = (
    "请用中文简要说明钢筋混凝土梁的受力特点，要求条理清晰、内容充实，"
    "涵盖正截面受弯、斜截面受剪、裂缝控制三个方面。"
)


def _one_call(client, model: str, max_tokens: int) -> tuple[float, int]:
    """发一次请求并计时。

    Args:
        client:     OpenAI 兼容客户端
        model:      模型名
        max_tokens: 生成上限（固定以保证各轮可比）

    Returns:
        (耗时秒, 实际生成的 token 数)
    """
    t0 = time.monotonic()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": _PROMPT}],
        max_tokens=max_tokens,
        temperature=0.0,
        seed=42,
        extra_body={"enable_thinking": False},
    )
    dt = time.monotonic() - t0
    out = resp.usage.completion_tokens if resp.usage else 0
    return dt, out


def probe(base_url: str, model: str, levels: list[int], max_tokens: int) -> None:
    """在多个并发档位上测吞吐，打印对比表并给出判读。

    Args:
        base_url:   vLLM endpoint
        model:      模型名
        levels:     并发档位列表，如 [1, 2, 4, 8]
        max_tokens: 每次生成的 token 上限

    Returns:
        None（结果打印到标准输出）
    """
    from openai import OpenAI

    api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    client = OpenAI(api_key=api_key, base_url=base_url)

    print(f"endpoint : {base_url}")
    print(f"model    : {model}")
    print(f"max_tokens: {max_tokens}　（LLM_RPM 环境变量当前值：{os.getenv('LLM_RPM', '未设置，默认 60')}）")
    print("注意：本探针**直连 OpenAI 客户端**，绕过 llm.py 的限流器——")
    print("      若此处并发有效而实际脚本无效，瓶颈就在 llm.py 的 _RateLimiter。\n")

    print(f"{'并发':>4} {'总耗时(s)':>10} {'单请求均值(s)':>14} {'总输出tok':>10} {'聚合tok/s':>11} {'相对1并发':>10}")
    print("-" * 66)

    base_tps = None
    for n in levels:
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(lambda _: _one_call(client, model, max_tokens), range(n)))
        wall = time.monotonic() - t0
        lat = statistics.mean(r[0] for r in results)
        tok = sum(r[1] for r in results)
        tps = tok / wall if wall else 0
        if base_tps is None:
            base_tps = tps
        print(f"{n:>4} {wall:>10.1f} {lat:>14.1f} {tok:>10} {tps:>11.1f} {tps/base_tps:>9.2f}x")

    print("-" * 66)
    print("判读：")
    print("  · 聚合 tok/s 随并发近似线性增长 → 服务端批处理正常，瓶颈在客户端（查 llm.py 限流器）")
    print("  · 聚合 tok/s 基本不变、单请求延迟随并发成倍增长 → 服务端在排队")
    print("    （vLLM 的 --max-num-seqs 过小，或该 GPU 被其他负载占满）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://172.19.2.2:8001/v1")
    parser.add_argument("--model", default="/models/Qwen3-32B-AWQ")
    parser.add_argument("--levels", default="1,2,4,8", help="并发档位，逗号分隔")
    parser.add_argument("--max-tokens", type=int, default=200)
    args = parser.parse_args()

    probe(args.base_url, args.model,
          [int(x) for x in args.levels.split(",")], args.max_tokens)
