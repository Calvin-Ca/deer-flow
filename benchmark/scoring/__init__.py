"""benchmark 打分器（纯函数，可单测、可进 CI）。

把「怎么判一次 agent 运行过没过」从「怎么跑 agent / 怎么挂 Langfuse」里剥出来：
runner 负责驱动 agent 拿观测，本包负责据观测程序化判定终态 + 红线 + pass^k 聚合。
"""
