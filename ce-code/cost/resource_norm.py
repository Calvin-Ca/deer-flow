"""cost/resource_norm.py —— 资源（工料机）名称/规格/单位归一化纯函数。

定额 resource 与信息价物料同物异名（定额把规格嵌进 name=「干混砌筑砂浆 M7.5」、信息价
name/spec 分离=「干混砌筑砂浆」+「M7.5」；×↔x、括号/空格、单位 千块↔块×1000 等系统性差异）
→ 直接精确匹配命中率极低。本模块产**确定性归一键**供 `resource_price_map` matcher 对齐两侧，
不含模型、不猜：归一后仍不同即判不匹配（残余交语义匹配/HITL）。

纯函数、无副作用、可独立单测（`tests/`）。
"""
from __future__ import annotations

import re

# 定额单位 → 信息价单位 的换算系数（amount = 含量 × 信息价单价 × factor）。键用归一后单位。
# 仅登确定可换算的对；未登 = 单位不兼容（不猜价，红线）。
_UNIT_FACTORS: dict[tuple[str, str], float] = {
    ("千块", "块"): 1000.0,
    ("百块", "块"): 100.0,
    ("千米", "m"): 1000.0,
    ("千米", "米"): 1000.0,
}


def normalize_text(s: str | None) -> str:
    """把名称/规格归一为匹配用键：小写、×→x、全角→半角、去括号空格、统一 Φ。

    参数：s —— 原始文本（name 或 name+spec 拼接），None/空 → ''。
    返回：归一化字符串（仅供相等比较，不可读回原文）。
    """
    if not s:
        return ""
    s = s.lower()
    # 符号统一：乘号、全角括号、希腊 Φ、波浪号
    s = (s.replace("×", "x").replace("（", "(").replace("）", ")")
           .replace("φ", "Φ".lower()).replace("～", "~").replace("，", ","))
    # 去括号与所有空白（含全角空格）
    s = re.sub(r"[()\s　]", "", s)
    return s


def canonical_key(name: str | None, spec: str | None) -> str:
    """资源匹配键 = 归一(name + spec)。

    定额侧规格嵌在 name（spec 多为 None），信息价侧 name/spec 分离——两侧都拼起来再归一，
    消除「拆没拆规格」的差异。
    参数：name —— 名称；spec —— 规格型号（可 None）。
    返回：归一化匹配键（str）。
    """
    return normalize_text((name or "") + (spec or ""))


def canonical_unit(u: str | None) -> str:
    """单位归一：清 LaTeX 残留（``$m^3$``）、上标 ``^3/^2``、``m3/m2`` → ``m³/m²``。

    定额表经 MinerU 抽取常带 ``$m^3$`` 之类 LaTeX 串，污染 quota_item/resource 的 unit
    （/quota、/price/compose 输出可见、也害单位匹配）。本函数把同义单位收敛到可比形态。
    参数：u —— 原始单位串，None/空 → ''。
    返回：归一化单位（str）。
    """
    if not u:
        return ""
    u = u.replace("$", "").replace("^3", "³").replace("^2", "²")
    u = u.replace("m3", "m³").replace("m2", "m²")
    return u.strip()


def unit_factor(quota_unit: str | None, price_unit: str | None) -> float | None:
    """定额单位 → 信息价单位 的价格换算系数（先各自归一再比）。

    参数：quota_unit —— 定额资源单位（如 千块/``$m^3$``）；price_unit —— 信息价单位（如 块）。
    返回：系数（float）使 amount = 含量 × 单价 × 系数；单位归一后相同→1.0；不可换算→None（红线：不猜）。
    """
    qu, pu = canonical_unit(quota_unit), canonical_unit(price_unit)
    if qu == pu:
        return 1.0
    return _UNIT_FACTORS.get((qu, pu))
