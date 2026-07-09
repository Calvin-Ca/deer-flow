"""任务层 LLM 调用公共件 —— 裸 Qwen3-8B vLLM JSON 直调。

从原 ``qa/generation.py:call_qwen3`` 抽出，供选码（``cost/selection.py``）与未来任务复用。
约定：Qwen3-8B 经 vLLM ``response_format=json_object`` 直出 JSON（非 function-calling，Qwen3-8B
function-calling 不可靠）；JSON 输出场景在 user message 末尾附 ``/no_think`` 禁 thinking。
"""
from __future__ import annotations

import json
from typing import Any

import requests


def call_qwen3(
    system_prompt: str,
    user_message: str,
    llm_url: str,
    model_id: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """调 vLLM chat/completions，强制 JSON 输出并解析为 dict。

    参数：
      system_prompt —— 系统提示（钉死输出契约/红线）；
      user_message —— 用户消息（JSON 场景末尾建议带 ``/no_think``）；
      llm_url —— vLLM 服务地址（如 ``http://localhost:8099``）；
      model_id —— 模型标识（如 ``qwen3-8b``）；
      temperature / max_tokens —— 采样参数。
    返回：
      解析后的 dict（``response_format=json_object`` 保证返回合法 JSON 对象）。
      vLLM 偶尔在 JSON 外包 markdown 代码块，已去壳；HTTP 非 2xx 经 ``requests.HTTPError`` 上抛，
      内容非法 JSON 经 ``json.JSONDecodeError`` 上抛。
    """
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    resp = requests.post(
        f"{llm_url}/v1/chat/completions",
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()

    raw_content = resp.json()["choices"][0]["message"]["content"].strip()

    # vLLM 偶尔在 JSON 外包一层 markdown 代码块，去掉它
    if raw_content.startswith("```"):
        lines = raw_content.splitlines()
        raw_content = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    return json.loads(raw_content)
