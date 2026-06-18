"""ingest —— PDF → chunks 摄取流水线（解析 parser + 切分 splitter + IR 契约 ir）。

组价知识库的上游：把规范/定额/信息价 PDF 解析、切分成 chunks.json，供 cost/ 抽取。
规范条文检索 RAG 已移除（2026-06-18 重构），本层只保留产出 chunks 的最小链路。
"""
