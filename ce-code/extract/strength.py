"""波1:黑体强条标注。对应 PRD §1/§4 的【P0】黑体强条 ≠ 语气强制。

拆两个正交字段:
  modal_strength       语气:必须/严禁/不应/不得/应/宜/可(纯正则,本文件可定)
  is_mandatory_clause  黑体法律强制(GB"强制性条文",召回率盯死的是它)

``is_mandatory_clause`` 有两条来源,按可靠性优先:
  ① 官方强制性条文清单(GB 50016 有官方清单,最可靠)—— load_official() 加载
  ② MinerU 版面分析字重信号(加粗/黑体)—— 字段名待服务器 dump 确认,
     候选键见 _BOLD_KEYS,确认后改这里即可,接口不变

二者都缺时,**保守**置 False(宁可后续靠语气补,也不误标),并记 source 供审计。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ── modal_strength(语气)──────────────────────────────────────────────────────

# "应"在 GB 规范是 shall;负向前瞻排除 应急/应对/应用… 等非情态用法
_APPLY_RE = re.compile(r"(?<![不无非])应(?!急|对|用|付|变|运|答|届|有尽有)")

# 优先级:硬强制(严禁/必须/不得/不应)> 应 > 宜 > 可;返回首个命中
_MODAL_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    ("严禁", re.compile(r"严禁|禁止")),
    ("必须", re.compile(r"必须")),
    ("不得", re.compile(r"不得")),
    ("不应", re.compile(r"不应")),
    ("应", _APPLY_RE),
    ("宜", re.compile(r"(?<!不)宜")),
    ("可", re.compile(r"(?<!不)可(?!能)")),
]


def parse_modal_strength(text: str) -> str:
    """正文 → 最强语气词;无命中返回空串。"""
    for label, pat in _MODAL_RULES:
        if pat.search(text):
            return label
    return ""


# ── is_mandatory_clause(黑体强制)────────────────────────────────────────────

# MinerU raw 里可能承载字重的候选键(服务器 dump content_list.json 后确认实际键名)
_BOLD_KEYS = ("bold", "is_bold", "font_weight", "weight", "text_level")


def load_official(path: str | Path | None) -> set[str]:
    """加载官方强制性条文清单(``["3.1.2", "5.3.4", ...]``)。无文件返回空集。"""
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    with open(p, encoding="utf-8") as f:
        return set(json.load(f))


def _bold_from_raw(raw: dict | None) -> bool | None:
    """从 MinerU raw 元素读字重;无可识别信号返回 None(交由调用方降级)。"""
    if not raw:
        return None
    for k in _BOLD_KEYS:
        if k in raw:
            v = raw[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return "bold" in v.lower() or v.strip() in {"700", "800", "900"}
            if isinstance(v, (int, float)):
                return v >= 700  # CSS font-weight 阈
    return None


def annotate_strength(clauses: list[dict], official_paths: set[str] | None = None) -> None:
    """原地写 ``modal_strength`` / ``is_mandatory_clause``(+ ``_strength_source`` 审计)。

    official_paths 为官方强条清单(优先);否则尝试字重;再否则保守 False。
    术语章(第2章)定义文字里的"应"不计强制——沿用 02 既有特判精神。
    """
    official = official_paths or set()
    for c in clauses:
        text = c.get("content", "")
        c["modal_strength"] = parse_modal_strength(text)

        path = c.get("clause_path", "")
        if path in official:
            c["is_mandatory_clause"] = True
            c["_strength_source"] = "official"
            continue

        bold = _bold_from_raw(c.get("raw"))
        if bold is not None:
            c["is_mandatory_clause"] = bool(bold)
            c["_strength_source"] = "bold"
            continue

        c["is_mandatory_clause"] = False
        c["_strength_source"] = "none"

    # 术语章:定义句不作强制(即便含"应")
    for c in clauses:
        if c.get("clause_path", "").startswith("2.") and c.get("_strength_source") != "official":
            c["is_mandatory_clause"] = False
