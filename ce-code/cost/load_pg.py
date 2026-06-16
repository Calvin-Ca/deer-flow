"""cost/load_pg.py —— 把 bill_spec.jsonl / aux_tables.jsonl 幂等导入 PostgreSQL。

替代服务器上手敲的 staging 表 + ``\\copy`` + ``INSERT ... j->>`` 展开那套（不可复现、易错）：
本脚本读 JSONL，用 psycopg 直连 ``ce_cost`` 库，按主键 ON CONFLICT DO UPDATE 幂等 upsert，
可重复执行不产生重复行。``--init-schema`` 先执行同目录 schema.sql 建表。

连接（按优先级）：``--dsn`` > 环境变量 ``CE_PG_DSN`` > 默认
``postgresql://cost@localhost:5433/ce_cost``（密码走 libpq 的 ``PGPASSWORD`` / ``~/.pgpass``，
不硬编码进仓库）。

跑法（服务器，单行）：
  uv run python -m cost.load_pg --init-schema --bill-spec data/structured/bill_spec.jsonl --aux data/structured/aux_tables.jsonl

依赖：psycopg（服务器装：``uv add 'psycopg[binary]'``）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from rich.console import Console

from cost.bill_spec import normalize_spec

console = Console()

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DSN = "postgresql://cost@localhost:5433/ce_cost"


def _read_jsonl(path: Path) -> list[dict]:
    """读 JSONL（一行一条 JSON）。

    参数：path —— .jsonl 文件路径。
    返回：dict 列表（空文件返回空列表）。
    """
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _backfill_doc(rec: dict) -> tuple[str, str]:
    """补 doc_id / spec_version（兼容归一前产出的旧 jsonl）。

    参数：rec —— 一条 bill_spec / aux 记录。
    返回：(doc_id, spec_version)；记录已带则直用，否则按 spec_version 反查归一。
    """
    if rec.get("doc_id") and rec.get("spec_version"):
        return rec["doc_id"], rec["spec_version"]
    return normalize_spec(rec.get("spec_version") or "")


def load_bill_spec(conn, records: list[dict]) -> int:
    """幂等 upsert bill_spec（按 code 主键）。

    参数：conn —— psycopg 连接；records —— bill_spec.jsonl 记录列表。
    返回：写入行数。
    """
    from psycopg.types.json import Jsonb

    rows = []
    for r in records:
        doc_id, spec_version = _backfill_doc(r)
        rows.append((
            r["code"], r.get("name", ""), r.get("unit") or None,
            r.get("calc_rule") or None,
            Jsonb(r.get("feature_schema") or []),
            Jsonb(r.get("work_content") or []),
            r.get("chapter") or None,
            Jsonb(r.get("provenance")) if r.get("provenance") is not None else None,
            doc_id, spec_version,
        ))
    sql = """
        INSERT INTO bill_spec
            (code, name, unit, calc_rule, feature_schema, work_content,
             chapter, provenance, doc_id, spec_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name, unit = EXCLUDED.unit,
            calc_rule = EXCLUDED.calc_rule, feature_schema = EXCLUDED.feature_schema,
            work_content = EXCLUDED.work_content, chapter = EXCLUDED.chapter,
            provenance = EXCLUDED.provenance, doc_id = EXCLUDED.doc_id,
            spec_version = EXCLUDED.spec_version
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_aux(conn, records: list[dict]) -> int:
    """幂等 upsert aux_table（按 doc_id + caption + chapter 唯一键）。

    参数：conn —— psycopg 连接；records —— aux_tables.jsonl 记录列表。
    返回：写入行数。
    """
    from psycopg.types.json import Jsonb

    rows = []
    for r in records:
        doc_id, spec_version = _backfill_doc(r)
        rows.append((
            r.get("chapter") or None, r.get("caption") or None, r.get("kind") or None,
            Jsonb(r.get("header") or []), Jsonb(r.get("body") or []),
            Jsonb(r.get("provenance")) if r.get("provenance") is not None else None,
            doc_id, spec_version,
        ))
    sql = """
        INSERT INTO aux_table
            (chapter, caption, kind, header, body, provenance, doc_id, spec_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_id, caption, chapter) DO UPDATE SET
            kind = EXCLUDED.kind, header = EXCLUDED.header, body = EXCLUDED.body,
            provenance = EXCLUDED.provenance, spec_version = EXCLUDED.spec_version
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_price_composition(conn, records: list[dict]) -> int:
    """幂等 upsert price_composition（按 doc_id + composite + seq 唯一键）。

    参数：conn —— psycopg 连接；records —— price_composition.jsonl 记录列表。
    返回：写入行数。
    """
    from psycopg.types.json import Jsonb

    rows = []
    for r in records:
        doc_id, spec_version = _backfill_doc(r)
        rows.append((
            r["composite"], r["kind"], r["seq"], r["component"],
            r.get("note") or None,
            Jsonb(r.get("provenance")) if r.get("provenance") is not None else None,
            doc_id, spec_version,
        ))
    sql = """
        INSERT INTO price_composition
            (composite, kind, seq, component, note, provenance, doc_id, spec_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_id, composite, seq) DO UPDATE SET
            kind = EXCLUDED.kind, component = EXCLUDED.component,
            note = EXCLUDED.note, provenance = EXCLUDED.provenance,
            spec_version = EXCLUDED.spec_version
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_resource(conn, records: list[dict]) -> int:
    """幂等 upsert resource（按 category+name+spec+unit 唯一键，NULLS NOT DISTINCT）。

    参数：conn —— psycopg 连接；records —— resource.jsonl 记录列表。
    返回：写入行数。
    """
    rows = [(r.get("res_code") or None, r["name"], r.get("spec") or None,
             r["category"], r["unit"], r.get("doc_id") or None) for r in records]
    sql = """
        INSERT INTO resource (res_code, name, spec, category, unit, doc_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (category, name, spec, unit) DO UPDATE SET
            res_code = EXCLUDED.res_code, doc_id = EXCLUDED.doc_id
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_quota_item(conn, records: list[dict]) -> int:
    """幂等 upsert quota_item（按 region+quota_code+spec_version 唯一键）。

    参数：conn —— psycopg 连接；records —— quota_item.jsonl 记录列表。
    返回：写入行数。
    """
    from psycopg.types.json import Jsonb

    rows = []
    for r in records:
        doc_id, spec_version = _backfill_doc(r)
        rows.append((
            r["quota_code"], r["name"], r.get("unit") or "",
            r.get("base_price"), r.get("labor_cost"), r.get("material_cost"),
            r.get("machine_cost"), r.get("work_content") or None, r.get("chapter") or None,
            Jsonb(r.get("provenance")) if r.get("provenance") is not None else None,
            doc_id, spec_version, r.get("region") or "深圳",
            r.get("effective_priority") or 1,
        ))
    sql = """
        INSERT INTO quota_item
            (quota_code, name, unit, base_price, labor_cost, material_cost,
             machine_cost, work_content, chapter, provenance, doc_id, spec_version,
             region, effective_priority)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (region, quota_code, spec_version) DO UPDATE SET
            name = EXCLUDED.name, unit = EXCLUDED.unit, base_price = EXCLUDED.base_price,
            labor_cost = EXCLUDED.labor_cost, material_cost = EXCLUDED.material_cost,
            machine_cost = EXCLUDED.machine_cost, work_content = EXCLUDED.work_content,
            chapter = EXCLUDED.chapter, provenance = EXCLUDED.provenance,
            effective_priority = EXCLUDED.effective_priority
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_quota_resource(conn, links: list[dict]) -> tuple[int, int]:
    """把 natural-key 含量链接解析成 (quota_id, resource_id) 后幂等 upsert quota_resource。

    依赖 resource / quota_item 已先入库：查回 id 建映射，再解析。quota_id 按
    (doc_id, quota_code) 消歧（171/170 同编码不撞），resource_id 按
    (category, name, spec=NULL, unit)。

    参数：conn —— psycopg 连接；links —— quota_resource.jsonl 链接列表。
    返回：(写入行数, 跳过行数)；任一端 id 解析不到则跳过。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, category, name, spec, unit FROM resource")
        rmap = {(c, n, s, u): i for i, c, n, s, u in cur.fetchall()}
        cur.execute("SELECT id, doc_id, quota_code FROM quota_item")
        qmap = {(doc, code): i for i, doc, code in cur.fetchall()}

    rows, skipped = [], 0
    for l in links:
        qid = qmap.get((l.get("doc_id"), l["quota_code"]))
        rid = rmap.get((l["category"], l["resource_name"], None, l["resource_unit"]))
        if qid is None or rid is None:
            skipped += 1
            continue
        rows.append((qid, rid, l["consumption"]))
    sql = """
        INSERT INTO quota_resource (quota_id, resource_id, consumption)
        VALUES (%s, %s, %s)
        ON CONFLICT (quota_id, resource_id) DO UPDATE SET consumption = EXCLUDED.consumption
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows), skipped


def load_bill_quota_map(conn, records: list[dict]) -> int:
    """幂等 upsert bill_quota_map（按 bill_code+quota_code+quota_doc_id 唯一键）。

    参数：conn —— psycopg 连接；records —— bill_quota_map.jsonl 记录列表。
    返回：写入行数。
    """
    rows = [(r["bill_code"], r["quota_code"], r["quota_doc_id"],
             r.get("relation") or "APPLIES", r.get("confidence"),
             r.get("source") or None, r.get("note") or None) for r in records]
    sql = """
        INSERT INTO bill_quota_map
            (bill_code, quota_code, quota_doc_id, relation, confidence, source, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (bill_code, quota_code, quota_doc_id) DO UPDATE SET
            relation = EXCLUDED.relation, confidence = EXCLUDED.confidence,
            source = EXCLUDED.source, note = EXCLUDED.note
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


@click.command()
@click.option("--dsn", default="", help="PG 连接串；空=环境变量 CE_PG_DSN 或默认 localhost:5433/ce_cost。")
@click.option("--init-schema", is_flag=True, help="先执行 schema.sql 建表（幂等）。")
@click.option("--bill-spec", "bill_spec_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="bill_spec.jsonl 路径。")
@click.option("--aux", "aux_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="aux_tables.jsonl 路径。")
@click.option("--price-composition", "price_comp_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="price_composition.jsonl 路径。")
@click.option("--resource", "resource_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="resource.jsonl 路径（定额工料机主数据）。")
@click.option("--quota-item", "quota_item_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="quota_item.jsonl 路径（定额子目）。")
@click.option("--quota-resource", "quota_res_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="quota_resource.jsonl 路径（子目×资源含量）。")
@click.option("--bill-quota-map", "bq_map_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="bill_quota_map.jsonl 路径（清单→定额 APPLIES）。")
def main(dsn: str, init_schema: bool, bill_spec_path: Path | None,
         aux_path: Path | None, price_comp_path: Path | None,
         resource_path: Path | None, quota_item_path: Path | None,
         quota_res_path: Path | None, bq_map_path: Path | None) -> None:
    """JSONL → PostgreSQL 幂等导入（建表 + 清单/定额/费用构成各表，单事务提交）。"""
    import psycopg

    dsn = dsn or os.environ.get("CE_PG_DSN") or DEFAULT_DSN
    console.print(f"[bold]连接[/] {dsn}")

    with psycopg.connect(dsn) as conn:
        if init_schema:
            conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            console.print(f"[green]✓ schema 已建/对齐[/]（{SCHEMA_SQL.name}）")
        if bill_spec_path:
            n = load_bill_spec(conn, _read_jsonl(bill_spec_path))
            console.print(f"[green]✓ bill_spec upsert {n} 条[/]")
        if aux_path:
            n = load_aux(conn, _read_jsonl(aux_path))
            console.print(f"[green]✓ aux_table upsert {n} 条[/]")
        if price_comp_path:
            n = load_price_composition(conn, _read_jsonl(price_comp_path))
            console.print(f"[green]✓ price_composition upsert {n} 条[/]")
        # 定额三表按依赖序：resource / quota_item 先入库，quota_resource 再解析 FK
        if resource_path:
            n = load_resource(conn, _read_jsonl(resource_path))
            console.print(f"[green]✓ resource upsert {n} 条[/]")
        if quota_item_path:
            n = load_quota_item(conn, _read_jsonl(quota_item_path))
            console.print(f"[green]✓ quota_item upsert {n} 条[/]")
        if quota_res_path:
            n, skip = load_quota_resource(conn, _read_jsonl(quota_res_path))
            console.print(f"[green]✓ quota_resource upsert {n} 条[/]"
                          + (f"，[yellow]跳过 {skip}（id 未解析）[/]" if skip else ""))
        if bq_map_path:
            n = load_bill_quota_map(conn, _read_jsonl(bq_map_path))
            console.print(f"[green]✓ bill_quota_map upsert {n} 条[/]")
        conn.commit()
    console.print("[bold green]✓ 提交完成[/]")


if __name__ == "__main__":
    main()
