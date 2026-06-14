"""tools —— 评测 / 审核 / 调试 CLI（非数据主链；从 ce-code 根以 ``-m tools.X`` 运行）。

  retrieve_cli   混合检索单查询调试 CLI（薄封装 retrieval.HybridRetriever，看单条 query 命中）。
  eval           检索质量评测（跑 data/eval_set/，期望/关联召回率；**按包含关系判命中**）。
                 ⚠️ 强条口径已清，T10 待换 Recall@k / 引用召回 / MRR。
  review_quality 节点库质量审核。⚠️ 仍 v1 口径（读旧 _clauses.json、统计强条），未适配
                 chunks.json，T10 待改为节点树健康检查（孤儿 / 空内容 / 表格归属 / 悬空引用）。

另含运维脚本：setup_server.sh（服务器一次性环境准备）、rename_raw_files.sh（原始 PDF 重命名）。
"""
