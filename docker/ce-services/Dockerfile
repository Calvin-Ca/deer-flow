# 建筑领域 CE —— 任务服务镜像（:8101）
#
# 承载：CostAgent（选码/组价）+ HITL 可中断组价图（langgraph + SqliteSaver）+ Norm-QA + 前门路由 +
#   ce-task MCP façade。纯 HTTP/编排层，不依赖 torch/Milvus，镜像轻量。
#
# 构建（从项目根执行）：
#   docker build -f docker/ce-services/Dockerfile -t rag-tasks .
FROM python:3.12-slim

# pip 源（默认清华，build 加速；可 --build-arg PIP_INDEX_URL=... 覆盖回官方）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 运行时依赖（⚠ 手工镜像 ce-services/pyproject.toml —— 那边加依赖这里必须跟，否则容器缺包
#   而宿主 pytest 全绿（07-05 实锤：openpyxl 只进了 pyproject，导出 xlsx 在容器里 501）。
#   TODO(契约单一源)：改为 COPY pyproject + uv sync 消灭手抄）：
#   langgraph + checkpoint-sqlite → HITL 可中断图；mcp → :8101/mcp 的 ce-task façade。缺则 import 即崩。
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "requests>=2.32" \
    "pydantic>=2.7" \
    "langgraph>=0.2.50" \
    "langgraph-checkpoint-sqlite>=2.0" \
    "mcp>=1.2" \
    "openpyxl>=3.1"

WORKDIR /app

# 运行时包（main.py 实际 import 的四个：common/cost/norm/routing；tools/ 是离线评测脚本，不入镜像）
COPY ce-services/main.py    main.py
COPY ce-services/common/    common/
COPY ce-services/cost/      cost/
COPY ce-services/norm/      norm/
COPY ce-services/routing/   routing/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8101"]
