"""
条款号抽取与归一化。

归一化目标格式：GB50010-2010_8.2.1（标准号_条款号）

支持的原始书写形式：
  GB50010-2010 第 8.2.1 条
  GB 50010—2010 8.2.1
  《混规》第8.2.1条
  GB50010 8.2
  第 3.1.1 条（上下文已知标准时可单独匹配）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 中文简称 → 标准号（不含年份，归一化时补）────────────────────────────
_ALIAS: dict[str, str] = {
    "混规":  "GB50010",
    "抗规":  "GB50011",
    "地规":  "GB50007",
    "荷规":  "GB50009",
    "高规":  "JGJ3",
    "混凝土规范": "GB50010",
    "抗震规范":   "GB50011",
    "地基规范":   "GB50007",
    "荷载规范":   "GB50009",
}

# ── 标准号 → 默认年份（PDF 解析时如无年份可补全）────────────────────────
_DEFAULT_YEAR: dict[str, str] = {
    "GB50010": "2010",
    "GB50011": "2010",
    "GB50007": "2011",
    "GB50009": "2012",
    "JGJ3":    "2010",
}

# ── 正则：标准号（带/不带年份，支持全角破折号）────────────────────────────
_STD_PATTERN = r"""
    (?:
        (?P<std>
            (?:GB\s*[/T]?\s*\d{4,5}   # GB 50010 / GB/T 50010
            |JGJ\s*\d+                 # JGJ3
            )
        )
        (?:[—\-]\s*(?P<year>\d{4}))?   # 可选年份
    )
"""

# ── 正则：条款号 x.y(.z)(.w) ────────────────────────────────────────────
_CLAUSE_NO = r"\d+(?:\.\d+){1,3}"

# ── 合并正则（先尝试带标准号，再尝试纯条款号）────────────────────────────
_RE_FULL = re.compile(
    rf"""
    {_STD_PATTERN}          # 标准号（可选年份）
    \s*                     # 空格
    (?:第\s*)?              # 可选"第"
    (?P<clause>{_CLAUSE_NO})  # 条款号
    \s*(?:条|款)?           # 可选"条/款"
    """,
    re.VERBOSE | re.IGNORECASE,
)

_RE_ALIAS = re.compile(
    rf"""
    《?(?P<alias>{'|'.join(re.escape(k) for k in _ALIAS)})》?
    [\s第]*
    (?P<clause>{_CLAUSE_NO})
    \s*(?:条|款)?
    """,
    re.VERBOSE,
)

_RE_BARE_CLAUSE = re.compile(
    rf"(?:第\s*)(?P<clause>{_CLAUSE_NO})\s*条"
)


@dataclass
class ClauseRef:
    raw: str
    std_code: str      # e.g. "GB50010"
    year: str          # e.g. "2010"
    clause_no: str     # e.g. "8.2.1"

    @property
    def clause_id(self) -> str:
        """拼装全局唯一的条款 ID，形如 GB50010-2010_8.2.1。

        year 为空时**不能再插连字符**：调用方可能传入已含年份的标准号
        （clause_splitter 就传 default_std="GB50010-2010"），此时 year 解析为空，
        无条件拼接会得到 `GB50010-2010-_8.2.1`——多一个连字符，与条文库的
        clause_id 对不上。实测该畸形 ID 使条文库 397 个 refs 全部失配，
        gen_evalset 的跨条文题因此产出 0 条；group_d1 曾在消费端打
        `replace("-_","_")` 补丁绕过，但没修这里，于是新消费方再次中招。

        Returns:
            条款 ID 字符串
        """
        if not self.year:
            return f"{self.std_code}_{self.clause_no}"
        return f"{self.std_code}-{self.year}_{self.clause_no}"


def _normalize_std(std: str) -> str:
    return re.sub(r"\s+", "", std).upper().replace("/T", "")


def extract(text: str, default_std: str = "") -> list[ClauseRef]:
    """从 text 中抽取所有条款引用，返回去重后的 ClauseRef 列表。"""
    refs: list[ClauseRef] = []
    seen: set[str] = set()

    def _add(std: str, year: str, clause: str, raw: str) -> None:
        std = _normalize_std(std)
        if not year:
            year = _DEFAULT_YEAR.get(std, "")
        cid = f"{std}-{year}_{clause}" if year else f"{std}_{clause}"
        if cid not in seen:
            seen.add(cid)
            refs.append(ClauseRef(raw=raw, std_code=std, year=year, clause_no=clause))

    # 1. 带标准号的完整引用
    for m in _RE_FULL.finditer(text):
        _add(m.group("std"), m.group("year") or "", m.group("clause"), m.group(0))

    # 2. 中文简称引用
    for m in _RE_ALIAS.finditer(text):
        alias = m.group("alias")
        std = _ALIAS[alias]
        year = _DEFAULT_YEAR.get(std, "")
        _add(std, year, m.group("clause"), m.group(0))

    # 3. 纯"第 X.X.X 条"（上下文已知标准）
    if default_std:
        year = _DEFAULT_YEAR.get(_normalize_std(default_std), "")
        for m in _RE_BARE_CLAUSE.finditer(text):
            _add(default_std, year, m.group("clause"), m.group(0))

    return refs


def clause_f1(pred_ids: set[str], gold_ids: set[str]) -> dict[str, float]:
    """计算条款引用 F1（PRD §5.1）。"""
    tp = len(pred_ids & gold_ids)
    p = tp / len(pred_ids) if pred_ids else 0.0
    r = tp / len(gold_ids) if gold_ids else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4)}


def is_hallucinated(clause_id: str, valid_ids: set[str]) -> bool:
    """判断条款号是否为硬幻觉（不在条文库中）。"""
    return clause_id not in valid_ids
