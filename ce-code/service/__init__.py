"""service —— 知识服务的 HTTP 包装（检索原语，:8100）。

只剩 ``server.py`` 一个文件：把 ``retrieval`` 包的检索原语（search/expand/get_clause）
包成 HTTP 端点。生成（qa）与编排（compliance）已迁出到顶层 ``services/`` 任务层，
作为本服务的纯 HTTP 客户端。
"""
