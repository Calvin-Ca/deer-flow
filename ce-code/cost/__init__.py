"""cost —— 结构化造价数据轨（Phase C 起点；与规范类 RAG 流水线解耦）。

  bill_spec  从节点树 chunks.json 抽清单项规范入 bill_spec.jsonl，
             辅助/参数表分流入 aux_tables.jsonl（双出口，一次遍历）。
  schema.sql 关系库全表 DDL（bill_spec/aux_table/quota_*/resource*/hist_bill，
             含治理字段 doc_id/version/region/effective_priority），幂等可审计。
  load_pg    JSONL → PostgreSQL 幂等导入（替代手敲 staging+\\copy）。
"""
