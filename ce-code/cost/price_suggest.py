"""信息价缺失料的近似料启发式召回（询价推荐，宁缺毋造）。

纯函数模块（不依赖 psycopg / 数据库），可独立单测。背景：定额工料机约 90% 未登信息价
（``price_status=no_source``），且连 ``ILIKE %name%`` 子串都常不命中（"干混砌筑砂浆" 库里只有
"干混砂浆"，多字即 miss）。本模块提供**和清单套定额同构**的启发式：料名 n-gram 覆盖率 + 同类
（人材机不混）打分排序，推荐近似料的价供人工参考选取——即使不是精确匹配。

失败方向安全：阈值过严 → 少推荐（交人工干输），不会张冠李戴推错类料。语义/LLM 近似召回做接口
预留（``suggest_prices_llm``），当前不接入。
"""
from __future__ import annotations

from typing import Any

_MIN_COVERAGE = 0.34  # 目标名 2-gram 覆盖率阈值（约 1/3 命中才算近似，低于此不推荐）


def _norm(text: str | None) -> str:
    """归一料名用于近似比较：去空格/常见规格符号、全角转半角、小写。

    参数：text —— 原始名称。
    返回：归一后的比较串。
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            continue
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII → 半角
            ch = chr(code - 0xFEE0)
        if ch in " \t×x·*/、，,()（）[]【】":  # 空白与规格分隔噪声
            continue
        out.append(ch.lower())
    return "".join(out)


def ngrams(text: str | None, n: int = 2) -> set[str]:
    """取归一名的字符 n-gram 集合（默认 2-gram），用于覆盖率相似度。

    参数：text —— 名称；n —— gram 长度。
    返回：n-gram 字符串集合；名长不足 n 时退化为整串单元素集。
    """
    t = _norm(text)
    if not t:
        return set()
    if len(t) < n:
        return {t}
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _coverage(target_name: str | None, candidate_name: str | None) -> float:
    """目标名 n-gram 被候选名覆盖的比例（0~1）。

    参数：target_name —— 缺价目标料名；candidate_name —— 候选近似料名。
    返回：覆盖率；目标无 gram 时 0。
    """
    target_grams = ngrams(target_name)
    if not target_grams:
        return 0.0
    return len(target_grams & ngrams(candidate_name)) / len(target_grams)


def suggest_prices(
    target_name: str | None,
    target_category: str | None,
    candidates: list[dict[str, Any]],
    *,
    top_k: int = 5,
    min_coverage: float = _MIN_COVERAGE,
) -> list[dict[str, Any]]:
    """启发式近似料推荐：名称 n-gram 覆盖率 + 同类过滤，打分排序取 top-k。

    功能：给定缺价目标料与一个宽松召回的候选池，按名称近似度打分（同 category 才推荐，人材机不
        混），返回带 score/match/reason 的近似料候选（含其登载价），供询价 HITL 让用户参考选取。
    参数：target_name —— 缺价料名；target_category —— 目标类别（人工/材料/机械，用于同类过滤）；
        candidates —— 候选料池（每项含 name/category/price 等）；top_k —— 返回上限；
        min_coverage —— 覆盖率阈值（低于此不推荐，宁缺毋造）。
    返回：近似料候选列表（按 score 降序）；无达标近似料返回空列表。
    """
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if target_category and candidate.get("category") and candidate.get("category") != target_category:
            continue  # 跨类不推荐（人/材/机不混）
        coverage = _coverage(target_name, candidate.get("name"))
        if coverage < min_coverage:
            continue
        scored.append(
            {
                **candidate,
                "score": round(coverage, 3),
                "match": "heuristic_ngram",
                "reason": f"名称近似 {coverage:.0%}",
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


def suggest_prices_llm(target: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """（预留）用语义/LLM 判定近似料。

    功能：n-gram 覆盖率之外的近似召回预留槽（同物异名但字面不重叠，如"商砼"↔"商品混凝土"），
        与 bill_quota_enrich 的 semantic_llm、select_quota 的 agent 槽同思路。当前未接入。
    参数：target —— 目标料字段；candidates —— 候选池。
    返回：近似料候选列表（同 suggest_prices 结构）。
    异常：NotImplementedError —— 尚未接入，调用方应回退启发式。
    """
    raise NotImplementedError("price_suggest 的语义/LLM 近似召回尚未接入，当前用 suggest_prices 启发式")
