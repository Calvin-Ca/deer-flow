from __future__ import annotations

from typing import Any

from deerflow.config import (
    get_enabled_tracing_providers,
    get_tracing_config,
    validate_enabled_tracing_providers,
)


def _create_langsmith_tracer(config) -> Any:
    from langchain_core.tracers.langchain import LangChainTracer

    return LangChainTracer(project_name=config.project)


def get_langfuse_client() -> Any:
    """返回按本项目 tracing 配置初始化的 Langfuse 客户端（langfuse>=4 单例）。

    功能：给「非 callback」场景（如 feedback 回写 score、评测脚本读写 dataset）
        提供与 ``build_tracing_callbacks`` 完全一致的凭据与 host —— host 取
        ``LANGFUSE_BASE_URL``（见 ``tracing_config``），避免直接 ``get_client()``
        因 SDK 默认读 ``LANGFUSE_HOST`` 而连错地址。
    参数：无。
    返回：已配置的 ``Langfuse`` 客户端；当 langfuse 未启用或凭据不全时返回 ``None``。
    """
    config = get_tracing_config().langfuse
    if not config.is_configured:
        return None
    from langfuse import Langfuse

    # 同参数重复构造返回同一单例，不会重复建连。
    return Langfuse(
        secret_key=config.secret_key,
        public_key=config.public_key,
        host=config.host,
    )


def build_langfuse_trace_id(run_id: str | None) -> str | None:
    """由 ``run_id`` 确定性派生 Langfuse trace_id。

    功能：让「发起 run 时绑定的 trace」与「事后按 run_id 回写 score」共用同一个
        trace_id，无需把随机 trace_id 持久化。worker（建 trace）与 feedback 路由
        （回写 score）都调本函数，口径不漂移。
    参数：run_id —— run 的唯一 id（feedback 也以它为键），为空则视作不可追踪。
    返回：32 位 hex trace_id；当 langfuse 未启用或 ``run_id`` 为空时返回 ``None``。
    """
    if not run_id or "langfuse" not in get_enabled_tracing_providers():
        return None
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=run_id)


def _create_langfuse_handler(config, trace_id: str | None = None) -> Any:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    # langfuse>=4 通过客户端单例初始化项目凭据，LangChain callback 再挂到该单例。
    Langfuse(
        secret_key=config.secret_key,
        public_key=config.public_key,
        host=config.host,
    )
    kwargs: dict[str, Any] = {"public_key": config.public_key}
    if trace_id:
        # 自定义 root trace id 只能在构造期经 trace_context 传入（v4 不支持走 metadata）。
        kwargs["trace_context"] = {"trace_id": trace_id}
    return LangfuseCallbackHandler(**kwargs)


def build_tracing_callbacks(langfuse_trace_id: str | None = None) -> list[Any]:
    """Build callbacks for all explicitly enabled tracing providers.

    Args:
        langfuse_trace_id: 可选的确定性 root trace id（由 ``build_langfuse_trace_id``
            从 run_id 派生）。传入时 Langfuse 这条 run 会落到该 trace_id 上，使 feedback
            等事后场景能按 run_id 重算 trace_id 并回写 score。``None`` 时沿用随机 id。
    """
    validate_enabled_tracing_providers()
    enabled_providers = get_enabled_tracing_providers()
    if not enabled_providers:
        return []

    tracing_config = get_tracing_config()
    callbacks: list[Any] = []

    for provider in enabled_providers:
        if provider == "langsmith":
            try:
                callbacks.append(_create_langsmith_tracer(tracing_config.langsmith))
            except Exception as exc:  # pragma: no cover - exercised via tests with monkeypatch
                raise RuntimeError(f"LangSmith tracing initialization failed: {exc}") from exc
        elif provider == "langfuse":
            try:
                callbacks.append(_create_langfuse_handler(tracing_config.langfuse, langfuse_trace_id))
            except Exception as exc:  # pragma: no cover - exercised via tests with monkeypatch
                raise RuntimeError(f"Langfuse tracing initialization failed: {exc}") from exc

    return callbacks
