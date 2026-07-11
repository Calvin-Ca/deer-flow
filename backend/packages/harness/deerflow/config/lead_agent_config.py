"""Configuration for the lead agent (system prompt template selection)."""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# backend 根从本文件位置推出（.../backend/packages/harness/deerflow/config/lead_agent_config.py
# → parents[4] = backend），与 cwd / DEER_FLOW_PROJECT_ROOT 完全无关。deerflow-harness 包
# 实际内嵌于 backend/packages/ 下，此锚点在 dev / Docker / benchmark-runner 三态恒成立。
_BACKEND_ROOT = Path(__file__).resolve().parents[4]


class LeadAgentConfig(BaseModel):
    """Lead-agent runtime configuration.

    Currently only carries the system-prompt template override. Keeping it in a
    dedicated section leaves room for future lead-agent-only knobs without
    polluting the top-level ``AppConfig`` namespace.
    """

    system_prompt_path: str | None = Field(
        default=None,
        description=(
            "Path to a system-prompt template file for the default lead agent. "
            "Absolute, or relative (resolved cwd-independently against project "
            "root, then backend/, then repo root — see "
            "resolve_system_prompt_file). When null, the built-in "
            "SYSTEM_PROMPT_TEMPLATE is used. Lets production swap prompt variants "
            "by editing config.yaml (picked up on the next message) instead of "
            "patching code. The file must only use placeholders that are a subset "
            "of apply_prompt_template's format kwargs."
        ),
    )


def resolve_system_prompt_file(template_path: str) -> Path | None:
    """把 ``system_prompt_path`` 解析成实际存在的文件；解析不到返回 None（调用方定回退）。

    历史坑：旧实现只按 ``project_root()``（无 env 时 = ``Path.cwd()``）解析相对路径——
    cwd 不是预期目录时静默回退内置模板，且 trace 的 variant 标签仍打文件名，评测失真。
    现改为多基座依次探测，**与 cwd 无关**：

    1. 绝对路径 → 原样（存在才算数）；
    2. ``project_root()/path``（兼容旧行为与 DEER_FLOW_PROJECT_ROOT 显式覆盖）；
    3. ``backend 根/path``（从 ``__file__`` 推出，三态恒定的主锚点）；
    4. ``仓库根/path``（backend 根的上一级，容忍从仓库根视角写的路径）。

    加载方（prompt.py）与打标方（tracing/metadata.py）共用本函数，保证
    「variant 标签说的就是实际加载的」。
    """
    p = Path(template_path)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        try:
            from deerflow.config.runtime_paths import project_root

            candidates.append(project_root() / p)
        except Exception:  # DEER_FLOW_PROJECT_ROOT 指向不存在路径时 project_root 会抛
            logger.debug("project_root() unavailable while resolving %s", template_path, exc_info=True)
        candidates.append(_BACKEND_ROOT / p)
        candidates.append(_BACKEND_ROOT.parent / p)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None
