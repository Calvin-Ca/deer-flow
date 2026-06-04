"""合规编排 · 查询矩阵 —— 结构化参数 → 合规检索查询（被 orchestration 使用）。

规则逐字不变（原 ce-code/service/queries.py 平移而来）：通用维度始终生成，
特定维度按参数条件触发。
"""
from __future__ import annotations

from typing import Any


def gen_queries(params: dict[str, Any]) -> list[dict[str, str]]:
    """返回 [{"dimension": str, "query": str}, ...]，按合规维度展开。"""
    btype = params.get("building_type") or "建筑"
    category = params.get("building_category") or f"{btype}建筑"
    height = params.get("height_m")
    h_str = f"{height}米" if height else "未知高度"
    floors_ug = params.get("floors_underground") or 0
    special_uses = params.get("special_uses") or []

    is_highrise = _is_highrise(params)
    is_residential = "住宅" in btype

    queries: list[dict[str, str]] = []

    # ── 通用维度（任何建筑均需检查） ──────────────────────────────────────

    queries.append({
        "dimension": "建筑分类与耐火等级",
        "query": f"{btype}建筑高度{h_str}的建筑分类和耐火等级要求",
    })

    queries.append({
        "dimension": "防火间距",
        "query": f"{category}与相邻建筑的防火间距要求",
    })

    queries.append({
        "dimension": "防火分区",
        "query": f"{category}每个防火分区最大建筑面积限制",
    })

    queries.append({
        "dimension": "安全出口",
        "query": f"{category}每层安全出口最少数量和布置要求",
    })

    queries.append({
        "dimension": "疏散楼梯",
        "query": f"{h_str}{btype}疏散楼梯的类型和最小净宽度要求",
    })

    queries.append({
        "dimension": "疏散距离与走道宽度",
        "query": f"{category}疏散距离和疏散走道最小净宽度要求",
    })

    queries.append({
        "dimension": "消防车道",
        "query": f"{category}消防车道的设置要求",
    })

    queries.append({
        "dimension": "建筑构件耐火极限",
        "query": f"{category}主要建筑构件（墙、柱、楼板、梁）耐火极限要求",
    })

    # ── 高层特有维度 ────────────────────────────────────────────────────────

    if is_highrise:
        queries.append({
            "dimension": "消防车登高操作场地",
            "query": f"{category}消防车登高操作场地的设置要求",
        })

        queries.append({
            "dimension": "消防电梯",
            "query": f"{category}消防电梯的设置条件和要求",
        })

        queries.append({
            "dimension": "消防供电",
            "query": f"{category}消防用电设备的供电要求和配电线路敷设",
        })

    # ── 消防设施（按高度/类型触发） ─────────────────────────────────────────

    if is_residential and height and height > 21:
        queries.append({
            "dimension": "室内消火栓",
            "query": f"建筑高度大于21米住宅建筑室内消火栓系统设置要求",
        })
    elif not is_residential:
        queries.append({
            "dimension": "室内消火栓",
            "query": f"{category}室内消火栓系统设置要求",
        })

    queries.append({
        "dimension": "自动喷水灭火系统",
        "query": f"{category}自动喷水灭火系统设置条件",
    })

    queries.append({
        "dimension": "火灾自动报警系统",
        "query": f"{category}火灾自动报警系统设置要求",
    })

    # ── 地下建筑 ────────────────────────────────────────────────────────────

    if floors_ug > 0:
        queries.append({
            "dimension": "地下建筑防火分区",
            "query": "地下建筑防火分区最大建筑面积和安全出口要求",
        })

    # ── 特殊用途 ────────────────────────────────────────────────────────────

    for use in special_uses:
        if "车库" in use or "停车" in use:
            queries.append({
                "dimension": "汽车库防火",
                "query": "地下汽车库防火分区面积、安全出口数量和消防设施要求",
            })
        elif "商业" in use or "餐饮" in use or "娱乐" in use:
            queries.append({
                "dimension": "人员密集场所",
                "query": f"{use}人员密集场所疏散设计和安全出口要求",
            })

    # 去重（同维度只保留一条）
    seen: set[str] = set()
    deduped = []
    for q in queries:
        if q["dimension"] not in seen:
            seen.add(q["dimension"])
            deduped.append(q)

    return deduped


def _is_highrise(params: dict[str, Any]) -> bool:
    category = (params.get("building_category") or "").lower()
    if "高层" in category:
        return True
    height = params.get("height_m")
    btype = params.get("building_type") or ""
    if height:
        threshold = 27 if "住宅" in btype else 24
        return height > threshold
    return False
