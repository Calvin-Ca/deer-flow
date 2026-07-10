"""共享路径引导：把 backend/ 前插进 sys.path，使 runner 从仓库根跑时能 import app.*。

功能：gateway 平时以 backend/ 为 CWD 运行，故 `import app` 天然可达；但 benchmark
    runner 是从仓库根用 `uv run --project backend python benchmark/runner/xxx.py` 跑的，
    backend/ 不在 sys.path，config.yaml 里 `use: app.ce.io.excel_tools:...` 一加载工具即
    `ModuleNotFoundError: No module named 'app'`。任一 runner 在建 DeerFlowClient 前
    `import _paths` 即把 backend/ 补进 sys.path，一处修好全体 runner（免每次记着加
    PYTHONPATH=backend）。
参数：无（import 时执行副作用）。
返回：无。
"""

from __future__ import annotations

import sys
from pathlib import Path

# _paths.py 位于 benchmark/runner/ → parents[2] 即仓库根，其下 backend/ 是 app 包所在。
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
