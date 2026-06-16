"""cost —— 结构化造价数据轨（Phase C 起点；与规范类 RAG 流水线解耦）。

  bill_spec  从节点树 chunks.json 抽清单项规范入 bill_spec.jsonl，
             辅助/参数表分流入 aux_tables.jsonl（双出口，一次遍历）。
  schema.sql 关系库全表 DDL（bill_spec/aux_table/quota_*/resource*/hist_bill，
             含治理字段 doc_id/version/region/effective_priority），幂等可审计。
  load_pg    JSONL → PostgreSQL 幂等导入（替代手敲 staging+\\copy）。

产物布局：抽取产物按 **doc_id 分目录** 落 ``data/structured/<doc_id>/<表>.jsonl``
（多规范累积不互相覆盖；与 splitter 的 ``<原始规范名>/default/chunks.json`` 子目录
不撞）。跨规范关系产物（如 bill_quota_map）保持扁平在 ``data/structured/`` 下。
"""

from __future__ import annotations

from pathlib import Path


def resolve_doc_dir(base: Path, records: list[dict]) -> Path:
    """从记录的 ``doc_id`` 推断 per-doc 输出子目录 ``base/<doc_id>``。

    一份 chunks.json = 一部规范，记录应共享单一 doc_id；多于一个即抛错（防把
    两部规范的产物混进同一目录）。

    参数：base —— 结构化产物根目录；records —— 带 ``doc_id`` 的记录列表。
    返回：``base/<doc_id>``（未创建，由调用方 mkdir）。
    """
    docs = {r["doc_id"] for r in records if r.get("doc_id")}
    if len(docs) != 1:
        raise ValueError(f"期望单一 doc_id，实得 {sorted(docs) or '空'}")
    return base / next(iter(docs))
