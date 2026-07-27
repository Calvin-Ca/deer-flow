"""Claude(Anthropic 官方 SDK)调用封装 —— 评测集出题专用。

为什么单开一个模块而不并进 src/utils/llm.py：
    llm.py 是 OpenAI SDK 的封装，打的是本地 vLLM（合成用 Qwen3-32B、判官用 Qwen3-8B）。
    一个模块里塞两套 SDK、两种鉴权、两套错误类型，早晚出乱子。两条路各自独立。

为什么评测集要换厂商出题：
    B/C/D 三组训练数据由 Qwen3-32B 合成。若评测题也由它出，会有两个问题——
    ① 撞车：同模型同条文同类 prompt，题目措辞高度相似，泄漏检查（铁律 3）会大面积告警；
    ② 风格耦合：题目风格天然贴近 B/C/D 的训练数据而非 A 组（模板体），
       系统性偏袒 B/C/D，把 A→B 的提升放大。
    换成 Anthropic 的模型是彻底的去相关——不同厂商、不同训练数据、不同风格。

三个角色至此互不重叠：
    合成训练数据  Qwen3-32B-AWQ（本地，¥0）
    出评测题      claude-sonnet-5（本模块）
    最终判分      GPT-4o（阶段 5.5，与前两者都无关）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

from src.utils.llm import cost_tracker

# 默认模型。用户明确指定 Sonnet；如需更强可切 claude-opus-5（价格约 2.5 倍）。
DEFAULT_MODEL = os.getenv("CE_CLAUDE_MODEL", "claude-sonnet-5")

# 出题不是深度推理任务，用低 effort 压住思考 token。
# Sonnet 5 的 effort 默认 high，思考 token 计入输出计费，不设会让成本翻数倍。
DEFAULT_EFFORT = os.getenv("CE_CLAUDE_EFFORT", "low")


def register_pricing(usd_per_cny: float = 7.2) -> None:
    """把 Claude 的价格登记进 llm.cost_tracker 的价格表。

    llm.py 的价格表以「元/千 token」计，而 Anthropic 官方报价是「美元/百万 token」，
    故此处换算。未登记的模型名会落到默认价（按 qwen-max 计），凭空报出错误金额——
    今天已经在 qwen3-8b 上踩过一次，故新增模型必须登记。

    Args:
        usd_per_cny: 美元兑人民币汇率，仅用于成本估算展示

    Returns:
        None（就地修改 llm._PRICE_PER_1K）
    """
    from src.utils import llm

    def _cny_per_1k(usd_per_1m: float) -> float:
        return usd_per_1m / 1000 * usd_per_cny

    llm._PRICE_PER_1K.update({
        # Sonnet 5 introductory 价（$2/$10 每百万 token），2026-08-31 后恢复 $3/$15。
        # ⚠️ 过期后须回来改这两行，否则成本统计会低报约 1/3。
        "claude-sonnet-5": {
            "input": _cny_per_1k(2.0),
            "output": _cny_per_1k(10.0),
        },
        "claude-opus-5": {
            "input": _cny_per_1k(5.0),
            "output": _cny_per_1k(25.0),
        },
    })


def call(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int = 4096,
    effort: str | None = None,
    sample_id: str = "",
) -> str:
    """调用 Claude 生成一次回复。

    与 llm.call 的三处刻意差异（都是 Sonnet 5 的硬约束，不是风格选择）：
      1. **不传 temperature / top_p / top_k** —— Sonnet 5 对非默认采样参数直接返回 400。
         需要控制随机性时用 prompt，不用采样参数。
      2. **不传 seed** —— Anthropic API 无此参数。出题的可复现性靠 prompt + 条文固定，
         而非采样种子；这一点须写进 EXPERIMENT.md 的局限（铁律 7 只能部分满足）。
      3. **max_tokens 给足余量** —— Sonnet 5 默认开启自适应思考，思考 token 与正文
         共用 max_tokens 预算。按正文所需的 4 倍留，否则会在思考阶段就被截断、
         正文一个字都出不来。

    Args:
        prompt:     用户轮内容
        system:     系统提示
        model:      模型 ID，默认 DEFAULT_MODEL
        max_tokens: 输出上限（含思考 token）
        effort:     思考深度 low/medium/high/xhigh/max，默认 DEFAULT_EFFORT
        sample_id:  样本 ID，仅用于失败留痕

    Returns:
        模型输出的纯文本

    Raises:
        RuntimeError: 安全分类器拒答，或响应中没有文本内容
        anthropic.APIError 及其子类: 调用失败（由调用方决定重试或留痕）
    """
    import anthropic

    model = model or DEFAULT_MODEL
    effort = effort or DEFAULT_EFFORT
    client = anthropic.Anthropic()

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": effort},
    }
    if system:
        kwargs["system"] = system

    resp = client.messages.create(**kwargs)

    # 安全分类器可能拒答（HTTP 200 + stop_reason=refusal，content 为空或残缺）。
    # 必须在读 content 之前判断，否则 content[0] 会 IndexError。
    # 规范条文出题极不可能触发，但静默失败的代价太高，仍显式处理。
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude 拒答（sample_id={sample_id}）：{resp.stop_details}")

    if resp.usage:
        cost_tracker.add(model, resp.usage.input_tokens, resp.usage.output_tokens)

    text = "".join(b.text for b in resp.content if b.type == "text")
    if not text.strip():
        raise RuntimeError(
            f"Claude 返回空正文（sample_id={sample_id}，stop_reason={resp.stop_reason}）。"
            f"若 stop_reason 为 max_tokens，说明思考 token 吃光了预算，需调高 max_tokens 或降低 effort。"
        )
    return text
