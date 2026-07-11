"""CostAgent —— 构件 → 选码 → 组价（任务层算量计价主线）。

端到端：构件描述 → ce-rag ``/search/bill-match`` 召回清单候选 → 本层 LLM 在候选内**选码**（selection.py）
→ ce-db ``/price/compose`` 确定性组价。与 Norm-QA（规范问答）并列为任务层两条主线。

红线：选码只从候选里选、不造码、低置信 `need_review` 转 HITL；`no_source` 不杜撰；`spec` 必填；
组价为确定性公式（LLM 不算钱）。
"""
