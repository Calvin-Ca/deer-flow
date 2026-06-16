"""cost/load_pg.py —— 把 bill_spec.jsonl / aux_tables.jsonl 幂等导入 PostgreSQL。

替代服务器上手敲的 staging 表 + ``\\copy`` + ``INSERT ... j->>`` 展开那套（不可复现、易错）：
本脚本读 JSONL，用 psycopg 直连 ``ce_cost`` 库，按主键 ON CONFLICT DO UPDATE 幂等 upsert，
可重复执行不产生重复行。``--init-schema`` 先执行同目录 schema.sql 建表。

连接（按优先级）：``--dsn`` > 环境变量 ``CE_PG_DSN`` > 默认
``postgresql://cost@localhost:5433/ce_cost``（密码走 libpq 的 ``PGPASSWORD`` / ``~/.pgpass``，
不硬编码进仓库）。

跑法（服务器，单行）：产物按 doc_id 分目录（``data/structured/<doc_id>/<表>.jsonl``），
``--scan-dir`` 自动扫全部规范 + 扁平 bill_quota_map 一把灌（依赖序在内已排）：
  uv run python -m cost.load_pg --init-schema --scan-dir data/structured

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


def load_resource_price(conn, records: list[dict]) -> tuple[int, int]:
    """信息价物料 → 先 upsert resource 取 id，再幂等写 resource_price（按时效区间）。

    `resource_price` 有 EXCLUDE（同资源+地区+来源时效不重叠）约束、不能 ON CONFLICT，
    故按 (doc_id, price_type, region, 时效区间) **先删后插**，保证同月重跑幂等。信息价
    物料即资源主数据的一个来源：`ON CONFLICT DO NOTHING` 与定额 resource 自然键合并、
    不覆盖定额行的 doc_id（命中即复用其 id，未命中则新建带 doc_id=SZ-JGXX-PRICE）。

    参数：conn；records —— resource_price.jsonl（含物料自然键 + price + 时效）。
    返回：(写入价行数, resource 解析失败跳过数)。
    """
    from datetime import date as _date

    from psycopg.types.range import Range

    def rng(r):
        return Range(_date.fromisoformat(r["effective_start"]),
                     _date.fromisoformat(r["effective_end"]), "[)")

    # 1. 信息价物料 upsert 进 resource（资源主数据来源之一；命中定额行则复用、不覆盖）
    res_rows = [(None, r["name"], r.get("spec") or None, r["category"],
                 r["unit"], r.get("doc_id") or None) for r in records]
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO resource (res_code, name, spec, category, unit, doc_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (category, name, spec, unit) DO NOTHING
        """, res_rows)
        cur.execute("SELECT id, category, name, spec, unit FROM resource")
        rmap = {(c, n, s, u): i for i, c, n, s, u in cur.fetchall()}

    # 2. 先删本期同来源价（幂等），再插
    with conn.cursor() as cur:
        for doc, pt, region, s, e in {(r["doc_id"], r["price_type"], r["region"],
                                       r["effective_start"], r["effective_end"])
                                      for r in records}:
            cur.execute("""DELETE FROM resource_price
                           WHERE doc_id=%s AND price_type=%s AND region=%s
                                 AND effective_period=%s""",
                        (doc, pt, region, Range(_date.fromisoformat(s),
                                                _date.fromisoformat(e), "[)")))

    rows, skipped = [], 0
    for r in records:
        rid = rmap.get((r["category"], r["name"], r.get("spec") or None, r["unit"]))
        if rid is None:
            skipped += 1
            continue
        rows.append((rid, r["region"], r["price"], r["price_type"], rng(r),
                     r.get("doc_id")))
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO resource_price
                (resource_id, region, price, price_type, effective_period, doc_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, rows)
    return len(rows), skipped


def load_fee_rate(conn, records: list[dict]) -> int:
    """幂等 upsert fee_rate（按 doc_id+fee_category+fee_name+applicable，NULLS NOT DISTINCT）。

    参数：conn —— psycopg 连接；records —— fee_rate.jsonl 记录列表。
    返回：写入行数。
    """
    from psycopg.types.json import Jsonb

    rows = [(r["fee_category"], r["fee_name"], r.get("applicable"),
             r.get("ref_low"), r.get("ref_high"), r.get("recommended"),
             r.get("unit") or "%",
             Jsonb(r.get("provenance")) if r.get("provenance") is not None else None,
             r.get("doc_id") or "SZ-FLBZ-2023", r["spec_version"],
             r.get("region") or "深圳", r.get("effective_priority") or 1)
            for r in records]
    sql = """
        INSERT INTO fee_rate
            (fee_category, fee_name, applicable, ref_low, ref_high, recommended,
             unit, provenance, doc_id, spec_version, region, effective_priority)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_id, fee_category, fee_name, applicable) DO UPDATE SET
            ref_low = EXCLUDED.ref_low, ref_high = EXCLUDED.ref_high,
            recommended = EXCLUDED.recommended, unit = EXCLUDED.unit,
            provenance = EXCLUDED.provenance, spec_version = EXCLUDED.spec_version
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


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


def _collect(scan_dir: Path | None, name: str, explicit: Path | None,
             flat: bool = False) -> list[Path]:
    """汇总一张表的待灌路径：scan_dir 下的 per-doc（或扁平）产物 + 显式单文件。

    参数：scan_dir —— 结构化产物根（None 则只用显式）；name —— 文件名；
    explicit —— 显式 ``--xxx`` 路径（可叠加）；flat —— True 则取 ``scan_dir/name``
    （跨规范关系产物如 bill_quota_map），否则取 ``scan_dir/*/name``（per-doc）。
    返回：去重保序的路径列表。
    """
    paths: list[Path] = []
    if scan_dir:
        if flat:
            p = scan_dir / name
            if p.exists():
                paths.append(p)
        else:
            paths.extend(sorted(scan_dir.glob(f"*/{name}")))
    if explicit:
        paths.append(explicit)
    seen, uniq = set(), []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp); uniq.append(p)
    return uniq


def _read_many(paths: list[Path]) -> list[dict]:
    """读多份 jsonl 并拼接。"""
    out: list[dict] = []
    for p in paths:
        out.extend(_read_jsonl(p))
    return out


@click.command()
@click.option("--dsn", default="", help="PG 连接串；空=环境变量 CE_PG_DSN 或默认 localhost:5433/ce_cost。")
@click.option("--init-schema", is_flag=True, help="先执行 schema.sql 建表（幂等）。")
@click.option("--scan-dir", "scan_dir", type=click.Path(exists=True, path_type=Path), default=None,
              help="结构化产物根：自动扫各 <doc_id>/ 子目录的全表 + 扁平 bill_quota_map.jsonl，"
                   "按依赖序一把灌（多规范累积）。可与下列单文件选项叠加。")
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
@click.option("--resource-price", "resource_price_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="resource_price.jsonl 路径（信息价物料月度价 + 时效）。")
@click.option("--fee-rate", "fee_rate_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="fee_rate.jsonl 路径（计价费率标准）。")
@click.option("--bill-quota-map", "bq_map_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="bill_quota_map.jsonl 路径（清单→定额 APPLIES）。")
def main(dsn: str, init_schema: bool, scan_dir: Path | None,
         bill_spec_path: Path | None, aux_path: Path | None,
         price_comp_path: Path | None, resource_path: Path | None,
         quota_item_path: Path | None, quota_res_path: Path | None,
         resource_price_path: Path | None, fee_rate_path: Path | None,
         bq_map_path: Path | None) -> None:
    """JSONL → PostgreSQL 幂等导入（建表 + 清单/定额/费用构成各表，单事务提交）。

    ``--scan-dir`` 扫各 ``<doc_id>/`` 子目录全表（多规范累积）+ 扁平 bill_quota_map，
    与显式单文件选项叠加；下方按 **依赖序** 灌：resource/quota_item 先于 quota_resource、
    bill_spec 先于 bill_quota_map（同一事务内后续查询可见先前插入，故 FK 解析成立）。
    """
    import psycopg

    dsn = dsn or os.environ.get("CE_PG_DSN") or DEFAULT_DSN
    console.print(f"[bold]连接[/] {dsn}")

    # 依赖序收集各表路径
    resource_paths = _collect(scan_dir, "resource.jsonl", resource_path)
    quota_item_paths = _collect(scan_dir, "quota_item.jsonl", quota_item_path)
    quota_res_paths = _collect(scan_dir, "quota_resource.jsonl", quota_res_path)
    bill_spec_paths = _collect(scan_dir, "bill_spec.jsonl", bill_spec_path)
    aux_paths = _collect(scan_dir, "aux_tables.jsonl", aux_path)
    price_comp_paths = _collect(scan_dir, "price_composition.jsonl", price_comp_path)
    resource_price_paths = _collect(scan_dir, "resource_price.jsonl", resource_price_path)
    fee_rate_paths = _collect(scan_dir, "fee_rate.jsonl", fee_rate_path)
    bq_map_paths = _collect(scan_dir, "bill_quota_map.jsonl", bq_map_path, flat=True)

    with psycopg.connect(dsn) as conn:
        if init_schema:
            conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            console.print(f"[green]✓ schema 已建/对齐[/]（{SCHEMA_SQL.name}）")
        # 定额三表按依赖序：resource / quota_item 先入库，quota_resource 再解析 FK
        if resource_paths:
            n = load_resource(conn, _read_many(resource_paths))
            console.print(f"[green]✓ resource upsert {n} 条[/]（{len(resource_paths)} 文件）")
        if quota_item_paths:
            n = load_quota_item(conn, _read_many(quota_item_paths))
            console.print(f"[green]✓ quota_item upsert {n} 条[/]（{len(quota_item_paths)} 文件）")
        if quota_res_paths:
            n, skip = load_quota_resource(conn, _read_many(quota_res_paths))
            console.print(f"[green]✓ quota_resource upsert {n} 条[/]"
                          + (f"，[yellow]跳过 {skip}（id 未解析）[/]" if skip else ""))
        if bill_spec_paths:
            n = load_bill_spec(conn, _read_many(bill_spec_paths))
            console.print(f"[green]✓ bill_spec upsert {n} 条[/]（{len(bill_spec_paths)} 文件）")
        if aux_paths:
            n = load_aux(conn, _read_many(aux_paths))
            console.print(f"[green]✓ aux_table upsert {n} 条[/]（{len(aux_paths)} 文件）")
        if price_comp_paths:
            n = load_price_composition(conn, _read_many(price_comp_paths))
            console.print(f"[green]✓ price_composition upsert {n} 条[/]")
        # 信息价：物料 upsert 进 resource 后写月度价（依赖 resource 表存在；自带 resource upsert）
        if resource_price_paths:
            n, skip = load_resource_price(conn, _read_many(resource_price_paths))
            console.print(f"[green]✓ resource_price 写 {n} 条[/]"
                          + (f"，[yellow]跳过 {skip}（resource 未解析）[/]" if skip else ""))
        if fee_rate_paths:
            n = load_fee_rate(conn, _read_many(fee_rate_paths))
            console.print(f"[green]✓ fee_rate upsert {n} 条[/]")
        # bill_quota_map 依赖 bill_spec + quota_item 已在库，放最后
        if bq_map_paths:
            n = load_bill_quota_map(conn, _read_many(bq_map_paths))
            console.print(f"[green]✓ bill_quota_map upsert {n} 条[/]")
        conn.commit()
    console.print("[bold green]✓ 提交完成[/]")


if __name__ == "__main__":
    main()
