"""定额组价方案分组（把清单命中的 1:N 定额候选切成可替代方案，供任务层 HITL 选一套）。

纯函数模块（不依赖 psycopg / 数据库），可独立单测。数据背景见 ``cost/TABLES.md`` §2、§4：
``bill_quota_map`` 是清单→定额 1:N 平铺边，无方案分组字段，且混了「构成型子目」（一套综合单价
共同构成）与「可替代候选」（干混/湿拌、泵送/非泵送，需项目特征消歧选一个）。

**方案模型**：一个清单的完整组价 = 多个工序子目共同构成，其中某些工序有可替代做法。故一套方案
是**多子目组合**，多套方案是在可替代工序上各选不同做法组合出的不同整套。本模块用**确定性启发式**
（定额编号 ``6位-N`` 的 6 位前缀 = 同一工序/同表并排的可替代做法簇）把子目分成工序组：组内多条 =
可替代（选一），组内单条 = 必选构成（每套都含）；方案 = 必选全含 × 各可替代组笛卡尔积。无可替代
工序或组合规模过大（含糊）时保守返回单方案，避免误拆算错价（宁缺毋造）。

LLM 精判分组做接口预留（``refine_schemes_llm``），当前不接入。
"""
from __future__ import annotations

import itertools
from typing import Any

_ALT_TOP_K = 3  # 每个可替代工序组最多推荐的做法数
_MAX_SCHEMES = 8  # 最终方案候选数上限
_MAX_COMBOS = 200  # 笛卡尔积规模上限，超过则保守降级（交 LLM/variant_group，不强枚举）


def _quota_prefix(quota_code: str) -> str:
    """取定额编号 ``6位-N`` 的 6 位前缀（去 -N 组内序号）。

    功能：定额编号相邻 -N 是原书同一张表并排的可替代做法，同 6 位前缀即同一可替代做法簇（工序组）。
    参数：quota_code —— 定额编号（如 "010001-1"）。
    返回：6 位前缀字符串（如 "010001"）；无 -N 分隔时返回原串。
    """
    return (quota_code or "").split("-", 1)[0]


def _base_price_subtotal(quotas: list[dict[str, Any]]) -> float | None:
    """求一组定额子目的基价小计（``base_price`` 求和，全空则 None）。

    功能：给方案候选带一个参考基价小计，供前端展示/排序（非定稿综合单价）。
    参数：quotas —— 定额子目列表。
    返回：base_price 数值之和（保留两位）；无任一数值则 None。
    """
    total = 0.0
    seen = False
    for quota in quotas:
        value = quota.get("base_price")
        if isinstance(value, (int, float)):
            total += float(value)
            seen = True
    return round(total, 2) if seen else None


def _confidence(quota: dict[str, Any]) -> float:
    """取定额边 confidence 数值（用于组内做法排序/方案短板评分），缺失记 -1。"""
    value = quota.get("confidence")
    return float(value) if isinstance(value, (int, float)) else -1.0


def _top_k(quotas: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """按 confidence 降序取一个工序组里前 k 条可替代做法（稳定排序，保留同分原序）。"""
    return sorted(quotas, key=_confidence, reverse=True)[:k]


def _make_scheme(quotas: list[dict[str, Any]], *, strategy: str, scheme_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    """把一组选定子目打包成一个方案候选（scheme）。

    功能：一个方案由多条子目构成；scheme_id 用组合子目编码（供 HITL 回传选择），score 取该方案
        最弱子目的 confidence（短板决定整套可信度）。
    参数：quotas —— 该方案选定的定额子目列表；strategy —— 分组来源标签；scheme_id/name 可选覆盖。
    返回：方案候选 dict，含 scheme_id/name/score/strategy/quota_codes/quotas/base_price_subtotal。
    """
    quota_list = list(quotas)
    codes = [quota.get("quota_code") for quota in quota_list]
    confidences = [quota.get("confidence") for quota in quota_list if isinstance(quota.get("confidence"), (int, float))]
    return {
        "scheme_id": scheme_id or "|".join(str(code) for code in codes),
        "name": name or "＋".join(str(quota.get("name") or quota.get("quota_code")) for quota in quota_list),
        "score": min(confidences) if confidences else None,
        "strategy": strategy,
        "quota_codes": codes,
        "quotas": quota_list,
        "base_price_subtotal": _base_price_subtotal(quota_list),
    }


def group_quota_schemes(
    quotas: list[dict[str, Any]],
    *,
    alt_top_k: int = _ALT_TOP_K,
    max_schemes: int = _MAX_SCHEMES,
) -> list[dict[str, Any]]:
    """把清单命中的定额子目按「工序组 + 可替代做法」启发式组合成组价方案候选（schemes）。

    功能：确定性启发式（宁缺毋造）——按定额 6 位前缀分工序组：组内多条=可替代做法（选一，取
        top-k），组内单条=必选构成（每套都含）。方案 = 必选全含 × 各可替代组笛卡尔积，故每个方案
        是多子目组合。无可替代工序或组合规模超上限（含糊）→ 保守返回单方案（整套不拆），消歧留给
        未来 ``variant_group`` 字段 / LLM 精判（见 ``refine_schemes_llm``）。
    参数：quotas —— ``compose_price`` 组好的定额子目列表（含 quota_code/confidence/base_price 等）；
        alt_top_k —— 每个可替代组保留的做法数；max_schemes —— 方案候选数上限（按 score 取 top）。
    返回：方案候选列表；每项含 scheme_id/name/score/strategy/quota_codes/quotas/base_price_subtotal。
        空输入返回空列表；单方案时长度为 1（消费方据此自动降级、不触发多方案 HITL）。
    """
    quotas = [quota for quota in quotas if isinstance(quota, dict)]
    if not quotas:
        return []

    # 按 6 位前缀分工序组（保留原序）
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for quota in quotas:
        prefix = _quota_prefix(str(quota.get("quota_code", "")))
        if prefix not in groups:
            groups[prefix] = []
            order.append(prefix)
        groups[prefix].append(quota)

    fixed = [groups[prefix][0] for prefix in order if len(groups[prefix]) == 1]  # 必选构成子目
    alt_groups = [_top_k(groups[prefix], alt_top_k) for prefix in order if len(groups[prefix]) > 1]  # 可替代工序组

    combo_size = 1
    for group in alt_groups:
        combo_size *= len(group)

    # 无可替代工序，或组合规模过大（含糊，不强枚举）→ 保守单方案（整套）
    if not alt_groups or combo_size > _MAX_COMBOS:
        return [_make_scheme(quotas, strategy="single_full", scheme_id="__full__", name="整套组价（未拆分可替代方案）")]

    schemes: list[dict[str, Any]] = []
    for combo in itertools.product(*alt_groups):
        selected_ids = {id(quota) for quota in (*fixed, *combo)}
        # 按原 quotas 顺序还原这套方案的多子目组合
        ordered = [quota for quota in quotas if id(quota) in selected_ids]
        schemes.append(_make_scheme(ordered, strategy="heuristic_prefix_combo"))

    schemes.sort(key=lambda scheme: scheme["score"] if scheme["score"] is not None else -1.0, reverse=True)
    return schemes[:max_schemes]


def refine_schemes_llm(quotas: list[dict[str, Any]], bill: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """（预留）用 LLM 精判把 1:N 定额候选切分成可替代方案。

    功能：与 ``bill_quota_enrich`` 的 semantic_llm 边同思路，做方案分组的语义消歧接口预留——
        启发式 ``group_quota_schemes`` 对跨前缀实为可替代、或组合过大的情形保守降级，未来由 LLM
        在候选内判定真正的工序/可替代分组（候选内选、不适用返回空、不硬拆，同 enrich 三红线）。当前未接入。
    参数：quotas —— 定额子目列表；bill —— 清单项字段（供 LLM 上下文）。
    返回：方案候选列表（同 ``group_quota_schemes`` 结构）。
    异常：NotImplementedError —— 尚未接入，调用方应回退启发式。
    """
    raise NotImplementedError("scheme 的 LLM 精判分组尚未接入，当前用 group_quota_schemes 启发式")
