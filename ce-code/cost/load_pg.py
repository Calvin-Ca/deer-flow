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


@click.command()
@click.option("--dsn", default="", help="PG 连接串；空=环境变量 CE_PG_DSN 或默认 localhost:5433/ce_cost。")
@click.option("--init-schema", is_flag=True, help="先执行 schema.sql 建表（幂等）。")
@click.option("--bill-spec", "bill_spec_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="bill_spec.jsonl 路径。")
@click.option("--aux", "aux_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="aux_tables.jsonl 路径。")
def main(dsn: str, init_schema: bool, bill_spec_path: Path | None,
         aux_path: Path | None) -> None:
    """JSONL → PostgreSQL 幂等导入（建表 + bill_spec + aux_table，单事务提交）。"""
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
        conn.commit()
    console.print("[bold green]✓ 提交完成[/]")


if __name__ == "__main__":
    main()
