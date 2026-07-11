"""共享路径引导：让 benchmark runner 从仓库根跑时，import 与 runtime 路径解析都等价于 cwd=backend。

功能：gateway 生产态以 backend/ 为 CWD 运行，而 benchmark runner 是从仓库根用
    `uv run --project backend python benchmark/<层目录>/xxx.py` 跑的，导致两处 cwd 依赖翻车，
    任一 runner 在建 DeerFlowClient 前 `import _paths` 一次修好（免每次记着加 PYTHONPATH=backend）：
    ① `import app`：backend/ 不在 sys.path，config.yaml 里 `use: app.ce.io.excel_tools:...`
       一加载工具即 `ModuleNotFoundError: No module named 'app'` → 把 backend/ 前插进 sys.path。
    ② runtime 路径：`project_root()` 取 `Path.cwd()`（=仓库根）时 `.deer-flow` 状态目录会指错
       → 把 DEER_FLOW_PROJECT_ROOT 设成 backend/，让 project_root()/runtime_home()/resolve_path
       全部对齐生产（cwd=backend）。注：lead-agent 提示词已不依赖本对齐——现走
       `resolve_system_prompt_file()` 多基座解析（cwd 无关，见 benchmark/prompts/README.md），
       但状态目录/其他相对路径仍需要 ②。
    config.yaml / extensions_config.json 均有从 __file__ 推出的仓库根 legacy 兜底（与 cwd 无关），
    故设了 DEER_FLOW_PROJECT_ROOT=backend 后它们仍从仓库根正常加载，不受影响。
参数：无（import 时执行副作用）。
返回：无。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


class _McpTeardownNoiseFilter(logging.Filter):
    """压掉 MCP streamable_http 会话收尾的良性噪音（只影响终端可读性，不改行为）。

    背景：langchain-mcp-adapters 对 streamable_http 是**每次工具调用各开一个会话**，
    DeerFlowClient 同步驱动下这些会话的 asyncgen 在事件循环收尾时被跨 task finalize，
    间歇抛「cancel scope in a different task」/「asynchronous generator is already
    running」——mcp/anyio 上游收尾竞态，不吃分（逐条分数照常出），但批量跑每条刷一屏
    traceback 严重干扰归因。这些报错全部走 asyncio logger 的 ERROR 通道
    （default_exception_handler），按特征串窄过滤；其余 asyncio 错误照常放行。
    """

    _NEEDLES = (
        "an error occurred during closing of asynchronous generator",
        "Attempted to exit cancel scope in a different task",
        "asynchronous generator is already running",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text = f"{text} {record.exc_info[1]!r}"
        except Exception:  # noqa: BLE001 —— 过滤器绝不能反过来弄崩日志
            return True
        return not any(n in text for n in self._NEEDLES)


logging.getLogger("asyncio").addFilter(_McpTeardownNoiseFilter())

# _paths.py 位于 benchmark/_shared/ → parents[2] 即仓库根，其下 backend/ 是 app 包与 runtime 文件所在。
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if _BACKEND.is_dir():
    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))  # ① import app
    # ② runtime 路径基准对齐生产 cwd=backend；setdefault 不覆盖用户显式设置。
    os.environ.setdefault("DEER_FLOW_PROJECT_ROOT", str(_BACKEND))
