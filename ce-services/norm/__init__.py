"""Norm-QA —— 造价规范问答（任务层）。

构件/计量计价类自然语言问题 → 知识服务 :8100 ``/search`` 检索造价规范条文 → Qwen3 带引用作答。
端点 ``POST /norm/qa``（router.py），生成契约见 generation.py。与 CostAgent（选码组价）并列为
任务层两条主线，复用同一知识服务的不同原语（规范条文检索 vs 组价取数）。
"""
