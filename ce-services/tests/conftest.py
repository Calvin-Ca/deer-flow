"""pytest 路径配置：把 ce-services 根加进 sys.path（与 tools/ 各脚本的 sys.path.insert 同法）。

服务器跑法：``cd ce-services && uv run pytest tests/ -q``（pytest 在 dev 依赖组）。
本地无 pytest 时可直跑各测试模块：``python tests/test_routing_regression.py``。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
