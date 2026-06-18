"""cost/query.py —— 造价关系库（PG）只读取数访问层。

服务层（``service.cost_api``）与 CLI/测试共享的数据访问原语：给定 region + 编码，
从 ``ce_cost`` 库取「定额子目 + 工料机含量」等组价取数结果。与写入侧 ``load_pg.py``
分离（本文件只读、不建表不 upsert）。

连接（按优先级）：参数 ``dsn`` > 环境变量 ``CE_PG_DSN`` > 默认
``postgresql://cost@localhost:5433/ce_cost``（密码走 libpq 的 ``PGPASSWORD`` /
``~/.pgpass``，不硬编码进仓库）——口径与 ``load_pg`` 一致。

依赖：psycopg（服务器装：``uv add 'psycopg[binary]'``）。
"""
from __future__ import annotations

import os
from datetime import date

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://cost@localhost:5433/ce_cost"

# 工料机展示序：人工 → 材料 → 机械 → 其他
_CATEGORY_ORDER = "CASE r.category WHEN '人工' THEN 0 WHEN '材料' THEN 1 WHEN '机械' THEN 2 ELSE 3 END"


def resolve_dsn(dsn: str | None = None) -> str:
    """解析 PG 连接串。

    参数：dsn —— 显式连接串；None/空 时回落环境变量 CE_PG_DSN，再回落默认值。
    返回：最终连接串（str）。
    """
    return dsn or os.environ.get("CE_PG_DSN") or DEFAULT_DSN


def connect(dsn: str | None = None) -> psycopg.Connection:
    """打开一个 PG 连接（dict_row 行工厂，调用方负责关闭/用 with 管理）。

    参数：dsn —— 见 ``resolve_dsn``。
    返回：psycopg.Connection（查询结果以 dict 返回，键为列名）。
    """
    return psycopg.connect(resolve_dsn(dsn), row_factory=dict_row)


def get_quota(conn: psycopg.Connection, region: str, code: str) -> dict | None:
    """按地区 + 定额子目编号直取定额子目及其工料机含量（CONSUMES 关系）。

    参数：
      conn —— psycopg 连接（dict_row）。
      region —— 地区（如 "深圳"）。
      code —— 定额子目编号（如 "010001-3"）。
    返回：``{"item": {...定额字段...}, "resources": [{category,name,spec,unit,consumption}...]}``；
          子目不存在时返回 None。同一 (region, code) 多版本时按 effective_priority 升序（本地=1 优先）、
          spec_version 降序取一条。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, quota_code, name, unit, base_price, labor_cost, material_cost, "
            "machine_cost, work_content, chapter, doc_id, spec_version, region "
            "FROM quota_item WHERE region = %s AND quota_code = %s "
            "ORDER BY effective_priority ASC, spec_version DESC LIMIT 1",
            (region, code),
        )
        item = cur.fetchone()
        if item is None:
            return None
        cur.execute(
            "SELECT r.category, r.name, r.spec, r.unit, qr.consumption "
            "FROM quota_resource qr JOIN resource r ON r.id = qr.resource_id "
            "WHERE qr.quota_id = %s "
            f"ORDER BY {_CATEGORY_ORDER}, r.name",
            (item["id"],),
        )
        resources = cur.fetchall()
    return {"item": item, "resources": resources}


# 组价取数：清单 → 定额（带 confidence）→ 工料机含量 ⋈ 信息价单价。价来源两路：①直连
# （定额 resource 本身有信息价，自然键已精确相等，factor=1）②经 resource_price_map 对齐到
# 信息价物料（带 unit_factor 单位换算）——直连优先。价取「按期」时优先 on_date 命中区间；
# on_date 为 None 时取该资源最新可用期（lower(period) 最大）。amount = 含量 × 单价 × factor。
_COMPOSE_SQL = (
    "SELECT q.quota_code, m.quota_doc_id, q.name AS quota_name, q.unit AS quota_unit, "
    "       q.base_price, q.labor_cost, q.material_cost, q.machine_cost, "
    "       m.confidence, m.source, "
    "       r.category, r.name AS res_name, r.spec, r.unit AS res_unit, qr.consumption, "
    "       p.price AS unit_price, p.effective_period, p.price_type, p.factor AS unit_factor "
    "FROM bill_quota_map m "
    "JOIN quota_item q ON q.quota_code = m.quota_code AND q.doc_id = m.quota_doc_id "
    "JOIN quota_resource qr ON qr.quota_id = q.id "
    "JOIN resource r ON r.id = qr.resource_id "
    "LEFT JOIN resource_price_map rm ON rm.quota_resource_id = r.id "
    "LEFT JOIN LATERAL ("
    "    SELECT rp.price, rp.effective_period, rp.price_type, "
    "           CASE WHEN rp.resource_id = r.id THEN 1.0 ELSE rm.unit_factor END AS factor "
    "    FROM resource_price rp "
    "    WHERE rp.resource_id IN (r.id, rm.price_resource_id) AND rp.region = %(region)s "
    "      AND (%(on_date)s::date IS NULL OR rp.effective_period @> %(on_date)s::date) "
    "    ORDER BY (rp.resource_id = r.id) DESC, lower(rp.effective_period) DESC LIMIT 1"
    ") p ON true "
    "WHERE m.bill_code = %(code)s AND m.bill_spec_version = %(bill_spec_version)s "
    "  AND q.region = %(region)s "
    f"ORDER BY m.confidence DESC NULLS LAST, q.quota_code, {_CATEGORY_ORDER}, r.name"
)


def compose_price(conn: psycopg.Connection, region: str, code: str,
                  on_date: date | None = None,
                  spec_versions: list[str] | None = None) -> dict | None:
    """组价取数：清单项 → 适用定额 → 工料机含量 + 信息价单价（含小计）。

    取数链 bill_spec → bill_quota_map(APPLIES, 带 confidence) → quota_item → quota_resource
    → resource → resource_price（经 resource_price_map 对齐同物异名）。**红线**：信息价（~152 种
    常用大宗料）未登的定额材料约 90%（干混砂浆/电焊条/料石毛石铁钉等专项料），join 不到价的工料机
    ``unit_price=None`` + ``price_status="no_source"``（绝不杜撰价），交任务层 HITL 询价、本端点
    「只建议不定稿」。命中的工料机走 resource_price_map 单位换算（amount=含量×单价×unit_factor）。

    参数：
      conn —— psycopg 连接（dict_row）。
      region —— 地区（如 "深圳"），同时用于定额 region 过滤与信息价 region 取价。
      code —— 清单编码（GB 50854，9 位）。
      on_date —— 计价期（date）；None 时每个资源取最新可用信息价期（非当日，故 2026-05 期亦可命中）。
      spec_versions —— **国标版本隔离**：限定 bill_spec 只取这些 spec_version（如 2024→
        ["GB/T 50854-2024","GB/T 50856-2024"]，2013→["GB/T 50854-2013"]）；同 9 位码跨版本共存时
        据此选版本，避免串库（见 config.SPEC_REGISTRY）。None=不限版本（仅单版本库时安全）。
    返回：``{"bill": {...清单字段...}, "region", "on_date", "quota_count",
          "quotas": [{quota_code, name, unit, confidence, source, 人材机费,
                      "resources": [{category,name,spec,unit,consumption,unit_price,unit_factor,
                                     price_period,price_type,price_status,amount}...]}...]}``；
          price_status: matched（信息价命中）/ no_source（信息价无此料→HITL 询价）；
          清单项不存在→None；存在但无映射定额→quotas 为空列表（200，供任务层感知覆盖缺口）。
    """
    with conn.cursor() as cur:
        sql = ("SELECT code, name, unit, unit_options, chapter, doc_id, spec_version "
               "FROM bill_spec WHERE code = %s")
        params: list = [code]
        if spec_versions:                                  # 国标版本隔离：限定版本，避免跨版本同码串库
            sql += " AND spec_version = ANY(%s)"
            params.append(list(spec_versions))
        sql += " LIMIT 1"
        cur.execute(sql, params)
        bill = cur.fetchone()
        if bill is None:
            return None
        # 按解析出的清单版本过滤 bill_quota_map，避免同 9 位码跨版本共用映射（版本隔离）
        cur.execute(_COMPOSE_SQL, {"code": code, "region": region, "on_date": on_date,
                                   "bill_spec_version": bill["spec_version"]})
        rows = cur.fetchall()

    quotas: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row["quota_code"], row["quota_doc_id"])
        q = quotas.get(key)
        if q is None:
            q = {
                "quota_code": row["quota_code"],
                "quota_doc_id": row["quota_doc_id"],
                "name": row["quota_name"],
                "unit": row["quota_unit"],
                "base_price": row["base_price"],
                "labor_cost": row["labor_cost"],
                "material_cost": row["material_cost"],
                "machine_cost": row["machine_cost"],
                "confidence": row["confidence"],
                "source": row["source"],
                "resources": [],
            }
            quotas[key] = q
            order.append(key)
        price = row["unit_price"]
        period = row["effective_period"]
        factor = row["unit_factor"] if row["unit_factor"] is not None else 1
        # 含量 × 单价 × 单位换算系数 = 小计；无信息价（含「%」其他材料费、信息价未登料）→ amount 留空
        amount = round(row["consumption"] * price * factor, 2) if price is not None else None
        q["resources"].append({
            "category": row["category"],
            "name": row["res_name"],
            "spec": row["spec"],
            "unit": row["res_unit"],
            "consumption": row["consumption"],
            "unit_price": price,
            "unit_factor": float(factor) if price is not None else None,
            "price_period": str(period) if period is not None else None,
            "price_type": row["price_type"],
            "price_status": "matched" if price is not None else "no_source",
            "amount": amount,
        })
    return {
        "bill": bill,
        "region": region,
        "on_date": on_date.isoformat() if isinstance(on_date, date) else None,
        "quota_count": len(order),
        "quotas": [quotas[k] for k in order],
    }
