"""
LLM API 封装 — Qwen-Max via DashScope OpenAI-compatible endpoint。
提供：重试/指数退避、RPM 限流、成本统计、失败留痕。
"""

from __future__ import annotations

import json
import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ── 价格表（元/千 token，以调用时实际版本号为准，务必更新）──────────────
_PRICE_PER_1K: dict[str, dict[str, float]] = {
    "qwen-max":             {"input": 0.04, "output": 0.12},
    "qwen-max-2025-01-25":  {"input": 0.04, "output": 0.12},
    "qwen-plus":            {"input": 0.008, "output": 0.024},
    # 本地 vLLM 服务，无 API 费用。
    # 注意：未登记的模型名会落到 .get() 的默认价（按 qwen-max 计），
    # 本地模型漏登记会凭空报出假费用，新增本地模型务必在此登记。
    "Qwen3-32B-AWQ":           {"input": 0.0, "output": 0.0},
    "/models/Qwen3-32B-AWQ":   {"input": 0.0, "output": 0.0},  # 172.19.2.2:8001，合成用
    "qwen3-8b":                {"input": 0.0, "output": 0.0},  # localhost:8099，判官用
}
_DEFAULT_MODEL = "qwen-max"

# ── 失败日志目录 ──────────────────────────────────────────────────────────
_FAILED_DIR = Path(__file__).parent.parent.parent / "data" / "interim" / "failed"
_FAILED_DIR.mkdir(parents=True, exist_ok=True)


# ── 成本累加器（线程安全）──────────────────────────────────────────────────
class _CostTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total_cny: float = 0.0
        self.calls: int = 0
        self.tokens: dict[str, int] = {"input": 0, "output": 0}
        self.model_calls: dict[str, int] = {}

    def add(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = _PRICE_PER_1K.get(model, {"input": 0.04, "output": 0.12})
        cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1000
        with self._lock:
            self.total_cny += cost
            self.calls += 1
            self.tokens["input"] += input_tokens
            self.tokens["output"] += output_tokens
            self.model_calls[model] = self.model_calls.get(model, 0) + 1
        return cost

    def summary(self) -> dict[str, Any]:
        return {
            "total_cny": round(self.total_cny, 4),
            "calls": self.calls,
            "tokens": dict(self.tokens),
            "model_calls": dict(self.model_calls),
        }


cost_tracker = _CostTracker()


# ── 简单 RPM 限流（令牌桶）────────────────────────────────────────────────
class _RateLimiter:
    def __init__(self, rpm: int = 60) -> None:
        self._interval = 60.0 / rpm
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


_rate_limiter = _RateLimiter(rpm=int(os.getenv("LLM_RPM", "60")))


# ── OpenAI 客户端（DashScope endpoint）────────────────────────────────────
def _get_client(base_url: str | None = None) -> OpenAI:
    """构造 OpenAI 兼容客户端。

    Args:
        base_url: 显式指定的 endpoint。为 None 时读环境变量 LLM_BASE_URL，
                  再回落到 DashScope 官方地址。判官模型与合成模型可能部署在
                  不同 endpoint（如 32B 在 :8001、8B 在 :8099），故支持按次覆盖。

    Returns:
        OpenAI 客户端实例
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("请设置环境变量 DASHSCOPE_API_KEY")
    return OpenAI(
        api_key=api_key,
        base_url=base_url or os.getenv(
            "LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
    )


# ── 失败留痕 ─────────────────────────────────────────────────────────────
def _log_failure(
    sample_id: str,
    prompt: str,
    system: str,
    model: str,
    error: str,
    extra: dict | None = None,
) -> None:
    record = {
        "ts": datetime.utcnow().isoformat(),
        "sample_id": sample_id,
        "model": model,
        "error": error,
        "system": system[:200],
        "prompt": prompt[:500],
        **(extra or {}),
    }
    date_tag = datetime.utcnow().strftime("%Y%m%d")
    path = _FAILED_DIR / f"{date_tag}_failed.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── 单次调用（带重试）────────────────────────────────────────────────────
@retry(
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _call_once(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    seed: int,
    extra_body: dict | None = None,
) -> tuple[str, int, int]:
    _rate_limiter.acquire()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        seed=seed,
        extra_body=extra_body,
    )
    content = resp.choices[0].message.content or ""
    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0
    return content, in_tok, out_tok


def call(
    prompt: str,
    *,
    system: str = "你是一位土木工程规范专家，回答准确、引用条款号规范。",
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    seed: int = 42,
    sample_id: str = "",
    extra_meta: dict | None = None,
    extra_body: dict | None = None,
    base_url: str | None = None,
) -> str:
    """
    单次 LLM 调用。失败超过重试上限时记录到 data/interim/failed/ 并抛出异常。

    Args:
        base_url: 覆盖本次调用的 endpoint（判官与合成模型可能不同机不同端口）。
                  为 None 时走 LLM_BASE_URL 环境变量。其余参数见签名。

    Returns:
        模型输出文本
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    client = _get_client(base_url)
    try:
        text, in_tok, out_tok = _call_once(
            client, model, messages, max_tokens, temperature, seed, extra_body
        )
        cost_tracker.add(model, in_tok, out_tok)
        return text
    except Exception as exc:
        _log_failure(sample_id, prompt, system, model, str(exc), extra_meta)
        raise


def batch_call(
    items: list[dict],
    prompt_fn: Any,
    *,
    system: str = "你是一位土木工程规范专家，回答准确、引用条款号规范。",
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    seed: int = 42,
    progress: bool = True,
) -> list[dict]:
    """
    批量调用。每个 item 是一个 dict，prompt_fn(item) -> str 生成 prompt。
    返回列表，每项追加 "output" 字段；失败项追加 "error" 字段并留痕，不中止。
    """
    try:
        from tqdm import tqdm
        iterator = tqdm(items, disable=not progress)
    except ImportError:
        iterator = items  # type: ignore[assignment]

    results = []
    for item in iterator:
        sid = str(item.get("sample_id", item.get("clause_id", "")))
        try:
            text = call(
                prompt_fn(item),
                system=system,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                seed=seed,
                sample_id=sid,
            )
            results.append({**item, "output": text})
        except Exception as exc:
            results.append({**item, "output": None, "error": str(exc)})
    return results


def print_cost_summary() -> None:
    s = cost_tracker.summary()
    print(
        f"[LLM cost] 累计 {s['calls']} 次调用 | "
        f"输入 {s['tokens']['input']:,} tok / 输出 {s['tokens']['output']:,} tok | "
        f"预估费用 ¥{s['total_cny']:.2f}"
    )
