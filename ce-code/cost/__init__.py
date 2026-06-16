"""cost —— 结构化造价数据轨（Phase C 起点；与规范类 RAG 流水线解耦）。

  bill_spec  从节点树 chunks.json 抽清单项规范入 bill_spec.jsonl，
             辅助/参数表分流入 aux_tables.jsonl（双出口，一次遍历）。
"""
