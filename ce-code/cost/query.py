"""cost/query.py —— 造价关系库（PG）只读取数访问层。

服务层（``service.db_api``）与 CLI/测试共享的数据访问原语：给定 region + 编码，
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

from .price_suggest import ngrams as _price_ngrams
from .price_suggest import suggest_prices as _suggest_prices
from .quota_scheme import group_quota_schemes

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


def get_bill(conn: psycopg.Connection, code: str,
             spec_versions: list[str] | None = None) -> dict | None:
    """按清单编码精确取一条清单项规范（FR-C 核对原语：编码有效性 / 单位一致性判据）。

    功能：从 ``bill_spec`` 按 9 位编码取清单项（名称/单位/特征/工作内容），供任务层核对
      「这份清单」里的编码是否存在、单位是否与规范一致。
    参数：
      conn —— psycopg 连接（dict_row）。
      code —— 9 位清单编码。
      spec_versions —— 国标版本隔离（同 ``compose_price``）：限定 spec_version 集合；None=不限。
    返回：``{code, name, unit, unit_options, feature_schema, work_content, chapter, doc_id,
      spec_version}`` 或 None（该版本下查无此码）。
    """
    with conn.cursor() as cur:
        sql = ("SELECT code, name, unit, unit_options, feature_schema, work_content, "
               "chapter, doc_id, spec_version FROM bill_spec WHERE code = %s")
        params: list = [code]
        if spec_versions:
            sql += " AND spec_version = ANY(%s)"
            params.append(list(spec_versions))
        sql += " LIMIT 1"
        cur.execute(sql, params)
        return cur.fetchone()


def query_resource_price(conn: psycopg.Connection, name: str, region: str = "深圳",
                         period: str | None = None, category: str | None = None,
                         top_k: int = 10) -> list[dict]:
    """按名称模糊查信息价（FR-I01 价格取数原语：动态数据、与国标版本无关）。

    功能：``resource_price ⋈ resource`` 按名称 ILIKE 模糊匹配 + region 过滤，按期取价；
      缺期时每资源取最新可用期（DISTINCT ON + lower(period) DESC）。价格为登载值原样返回，
      不做任何加工换算（C-04：算差价/合价归任务层计算工具）。
    参数：
      conn —— psycopg 连接（dict_row）。
      name —— 材料/人工/机械名称（子串模糊，如「钢筋」「商品混凝土」）。
      region —— 地区（默认深圳）。
      period —— 期号 ``YYYY-MM``；None=各资源最新期。
      category —— 人工/材料/机械 过滤（可选）。
      top_k —— 返回行数上限（名称越短越贴切者优先）。
    返回：``[{name, spec, unit, category, price, price_type, period_start, period_end, doc_id}...]``；
      零命中返回空列表（诚实 no_source，调用方不据此编价）。
    """
    conds = ["rp.region = %(region)s", "r.name ILIKE %(pattern)s"]
    params: dict = {"region": region, "pattern": f"%{name}%", "top_k": top_k}
    if category:
        conds.append("r.category = %(category)s")
        params["category"] = category
    if period:
        # 期号 YYYY-MM → 该月首日落在时效区间内（信息价按月登载，effective_period=[月首,次月首)）
        conds.append("rp.effective_period @> %(period_date)s::date")
        params["period_date"] = f"{period}-01"
        distinct = ""
        order = "ORDER BY length(r.name) ASC, r.name"
    else:
        # 缺期：每资源取最新一期（DISTINCT ON 按 resource_id 保留 lower(period) 最大行）
        distinct = "DISTINCT ON (r.id) "
        order = "ORDER BY r.id, lower(rp.effective_period) DESC"
    sql = (
        f"SELECT * FROM (SELECT {distinct}r.name, r.spec, r.unit, r.category, "
        "rp.price, rp.price_type, lower(rp.effective_period) AS period_start, "
        "upper(rp.effective_period) AS period_end, rp.doc_id "
        "FROM resource_price rp JOIN resource r ON r.id = rp.resource_id "
        f"WHERE {' AND '.join(conds)} {order}) t "
        "ORDER BY length(t.name) ASC, t.name LIMIT %(top_k)s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    for r in rows:  # date/Decimal → JSON 可序列化
        r["price"] = float(r["price"]) if r["price"] is not None else None
        r["period_start"] = r["period_start"].isoformat() if r["period_start"] else None
        r["period_end"] = r["period_end"].isoformat() if r["period_end"] else None
    return rows


def suggest_resource_prices(conn: psycopg.Connection, name: str, region: str = "深圳",
                            category: str | None = None, top_k: int = 5,
                            pool_limit: int = 100) -> list[dict]:
    """近似料启发式询价：库中无精确/子串命中时，按名称 n-gram 宽松召回同类候选池后打分推荐。

    功能：``query_resource_price`` 的 ``ILIKE %name%`` 子串对 no_source 料常 miss（多字即不命中）。
      本函数用目标名的 2-gram 构造 ``ILIKE ANY`` 宽松召回候选池（任一 gram 命中），再交纯函数
      ``suggest_prices`` 按覆盖率 + 同类打分排序。和「清单套定额」同构的启发式近似匹配，宁缺毋造：
      仍无达标近似料则返回空（不硬推、不编价）。
    参数：
      conn —— psycopg 连接（dict_row）。
      name —— 缺价料名（定额工料机名，如「干混砌筑砂浆」）。
      region —— 地区（默认深圳）。
      category —— 人工/材料/机械 过滤（强烈建议带，避免跨类推荐）。
      top_k —— 返回近似料条数上限。
      pool_limit —— 宽松召回候选池规模上限（打分前）。
    返回：``[{name, spec, unit, category, price, price_type, period_start, period_end, doc_id,
          score, match, reason}...]``（按 score 降序）；无近似料返回空列表。
    """
    grams = _price_ngrams(name)
    if not grams:
        return []
    conds = ["rp.region = %(region)s", "r.name ILIKE ANY(%(patterns)s)"]
    params: dict = {"region": region, "patterns": [f"%{g}%" for g in grams], "pool_limit": pool_limit}
    if category:
        conds.append("r.category = %(category)s")
        params["category"] = category
    sql = (
        "SELECT DISTINCT ON (r.id) r.name, r.spec, r.unit, r.category, "
        "rp.price, rp.price_type, lower(rp.effective_period) AS period_start, "
        "upper(rp.effective_period) AS period_end, rp.doc_id "
        "FROM resource_price rp JOIN resource r ON r.id = rp.resource_id "
        f"WHERE {' AND '.join(conds)} "
        "ORDER BY r.id, lower(rp.effective_period) DESC LIMIT %(pool_limit)s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        pool = cur.fetchall()
    for r in pool:  # Decimal/date → JSON 可序列化
        r["price"] = float(r["price"]) if r["price"] is not None else None
        r["period_start"] = r["period_start"].isoformat() if r["period_start"] else None
        r["period_end"] = r["period_end"].isoformat() if r["period_end"] else None
    return _suggest_prices(name, category, pool, top_k=top_k)


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
    "       q.chapter AS quota_chapter, q.spec_version AS quota_spec_version, "
    "       m.confidence, m.source, "
    "       r.category, r.name AS res_name, r.spec, r.unit AS res_unit, qr.consumption, "
    "       p.price AS unit_price, p.effective_period, p.price_type, p.factor AS unit_factor, "
    "       p.price_doc_id "
    "FROM bill_quota_map m "
    "JOIN quota_item q ON q.quota_code = m.quota_code AND q.doc_id = m.quota_doc_id "
    "JOIN quota_resource qr ON qr.quota_id = q.id "
    "JOIN resource r ON r.id = qr.resource_id "
    "LEFT JOIN resource_price_map rm ON rm.quota_resource_id = r.id "
    "LEFT JOIN LATERAL ("
    "    SELECT rp.price, rp.effective_period, rp.price_type, rp.doc_id AS price_doc_id, "
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
          "quotas": [{quota_code, quota_doc_id, name, unit, confidence, source, 人材机费,
                      chapter, spec_version,   # 定额溯源：库号(quota_doc_id)+版本+章节，供 HITL 依据卡定位子目来源
                      "resources": [{category,name,spec,unit,consumption,unit_price,unit_factor,
                                     price_period,price_type,price_doc_id,price_status,amount}...]}...]}``；
          price_status: matched（信息价命中）/ no_source（信息价无此料→HITL 询价）；
          price_doc_id: 命中信息价的来源文件标识（如 SZ-JGXX-PRICE），与 price_period 期段共定位（行号待 ingest 补）；
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
                # 定额溯源（HITL 依据卡用）：库号 quota_doc_id + 版本 + 章节定位到具体子目来源
                "chapter": row["quota_chapter"],
                "spec_version": row["quota_spec_version"],
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
            # 信息价溯源（HITL 依据卡用）：来源文件 price_doc_id + 期段 price_period 定位到具体价（行号待 ingest 补）
            "price_doc_id": row["price_doc_id"] if price is not None else None,
            "price_status": "matched" if price is not None else "no_source",
            "amount": amount,
        })
    ordered_quotas = [quotas[k] for k in order]
    return {
        "bill": bill,
        "region": region,
        "on_date": on_date.isoformat() if isinstance(on_date, date) else None,
        "quota_count": len(order),
        "quotas": ordered_quotas,
        # 可替代定额方案候选（启发式分组，供任务层 select_quota HITL 选一套）；
        # 单方案时长度为 1，消费方自动降级不触发多方案确认。见 cost/quota_scheme.py。
        "schemes": group_quota_schemes(ordered_quotas),
    }
