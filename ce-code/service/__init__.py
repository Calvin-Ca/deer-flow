"""service —— 知识层 server-side 逻辑与 HTTP 服务。

包含两个独立服务的 FastAPI app 及其编排/生成逻辑（均 import retrieval 做检索）：
  server.py            检索服务 :8100（/search /expand /clause /qa /retrieve /health）
  compliance_server.py 合规服务 :8101（/compliance /health）
  generation.py        生成逻辑（被检索服务 /qa 使用）
  orchestration.py     合规编排（被合规服务使用）
  params.py / queries.py  参数提取 / 查询矩阵（被合规编排使用）
"""
