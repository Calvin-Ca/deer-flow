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
