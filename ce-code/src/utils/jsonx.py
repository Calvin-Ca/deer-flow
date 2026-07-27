"""从 LLM 输出里稳健地解析 JSON。

存在的理由：规范条文含大量 LaTeX（MinerU 产出即为此形式），模型作答时会照抄
`\\gamma`、`\\text{kN}`、`\\leqslant`，而 `\\g`、`\\t`、`\\l` 里只有 `\\t` 是合法
JSON 转义，其余一律让 `json.loads` 抛 JSONDecodeError。

这不是模型的错——照抄条文是合理行为，也不能靠 prompt 禁用 LaTeX（条文本身就是）。
只能在解析层修复。

实测影响面：
  评测集出题  400 题失败 72 题，失败率随数学密度递增
              （cross_clause 39% > calculation 18% > refusal 0%，拒答题不含公式）
  B 组合成    2357 条失败 174 条（7.4%），超过脚本自身 5% 的告警门线

抽成共享模块而非各处复制：判官实现曾因三处副本、换代只改一份而静默走回旧逻辑
（见 PROGRESS 阻塞表 B-10），同类错误不再重演。
"""
from __future__ import annotations

import json
import re
from typing import Any


def repair_escapes(blob: str) -> str:
    """把 JSON 串里的裸反斜杠补成合法转义。

    **必须按「转义对」为单位扫描，不能逐字符看后继**：模型写的 `\\\\gamma`
    （合法 JSON 转义，表示一个字面反斜杠）若用 `re.sub(r'\\\\(?![合法转义])')` 处理，
    首个反斜杠因后继是反斜杠被跳过、第二个被翻倍，结果 `\\\\\\gamma`——
    把本来合法的转义对拆坏，反而制造新错误。

    对 `\\n` `\\t` `\\r` 一类不作保留：LaTeX 命令 `\\frac` `\\times` `\\rho`
    恰好以这些字母开头，保留原义会把公式损坏成控制字符（`\\times` → 制表符 + "imes"），
    而 `\\u` 后接非十六进制更会直接解析失败。故除 `\\"` 与 `\\\\` 外一律转义为字面
    反斜杠：宁可让正文里真正的换行退化成字面 `\\n`，也不丢整条样本。

    Args:
        blob: 疑似含非法转义的 JSON 文本

    Returns:
        修复后的文本（未必可解析，由调用方兜底）
    """
    out: list[str] = []
    i = 0
    while i < len(blob):
        if blob[i] == "\\" and i + 1 < len(blob):
            if blob[i + 1] in '"\\':
                out.append(blob[i:i + 2])   # \" 和 \\ 原样保留
                i += 2
                continue
            out.append("\\\\")              # 其余一律转义成字面反斜杠
            i += 1
            continue
        out.append(blob[i])
        i += 1
    return "".join(out)


def loads_lenient(blob: str) -> Any | None:
    """先按原样解析，失败再修复转义重试。

    Args:
        blob: JSON 文本

    Returns:
        解析结果；两次都失败时返回 None
    """
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(repair_escapes(blob))
    except json.JSONDecodeError:
        return None


def extract(raw: str, kind: str = "object") -> Any | None:
    """从模型输出中截取 JSON 片段并解析（容忍 ``` 包裹与前后说明文字）。

    Args:
        raw:  模型原始输出
        kind: "object" 取最外层 {...}，"array" 取最外层 [...]

    Returns:
        解析结果；截取不到或解析失败时返回 None
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    pat = r"\{[\s\S]*\}" if kind == "object" else r"\[[\s\S]*\]"
    m = re.search(pat, cleaned)
    if not m:
        return None
    return loads_lenient(m.group(0))
