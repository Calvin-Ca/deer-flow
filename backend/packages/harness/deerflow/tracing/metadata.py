"""Langfuse trace-attribute metadata builders.

The Langfuse v4 ``langchain.CallbackHandler`` lifts a fixed set of reserved
keys from ``RunnableConfig.metadata`` onto the root trace:

- ``langfuse_session_id`` → groups traces (LangGraph thread → Langfuse Session)
- ``langfuse_user_id``    → trace user_id (powers the Users page)
- ``langfuse_trace_name`` → human-readable trace name
- ``langfuse_tags``       → trace tags

See ``langfuse/langchain/CallbackHandler.py::_parse_langfuse_trace_attributes``
and https://langfuse.com/docs/observability/features/sessions for the
contract. Builders here exist so the gateway/run worker can inject the
right metadata without leaking Langfuse internals into the call sites.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deerflow.config import get_enabled_tracing_providers

# Lazy-imported below to avoid a circular import: ``deerflow.runtime`` eagerly
# imports the run worker, which in turn needs ``deerflow.tracing``.
_DEFAULT_TRACE_NAME = "lead-agent"

# Label used when no system-prompt override is active (built-in template).
_DEFAULT_PROMPT_VARIANT = "default"


def resolve_active_prompt_variant(app_config: Any | None = None) -> str:
    """Return a short label for the active lead-agent system-prompt variant.

    Derived from ``lead_agent.system_prompt_path``: the file stem when an
    override is configured (e.g. ``v2_runbook_saturated``), or ``"default"``
    when the built-in template is in use. When ``app_config`` is None the global
    ``get_app_config()`` singleton is consulted (matching the embedded
    ``DeerFlowClient`` path); any failure degrades to ``"default"`` so tracing
    never raises.

    The label is emitted as a ``variant`` trace tag so prompt-ablation runs — and
    later production traffic — can be filtered/grouped by prompt version directly
    in LangSmith / Langfuse, decoupling request generation from offline scoring.
    """
    config = app_config
    if config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
        except Exception:
            return _DEFAULT_PROMPT_VARIANT

    lead_agent_config = getattr(config, "lead_agent", None)
    template_path = getattr(lead_agent_config, "system_prompt_path", None)
    if not template_path:
        return _DEFAULT_PROMPT_VARIANT
    return Path(template_path).stem


def build_langfuse_trace_metadata(
    *,
    thread_id: str | None,
    user_id: str | None = None,
    assistant_id: str | None = None,
    model_name: str | None = None,
    environment: str | None = None,
    variant: str | None = None,
) -> dict[str, Any]:
    """Return Langfuse trace-attribute metadata for ``RunnableConfig.metadata``.

    Returns ``{}`` when Langfuse is not in the enabled tracing providers so
    callers can unconditionally merge the result without affecting LangSmith
    or other tracers.

    Args:
        thread_id: LangGraph thread id; mapped to ``langfuse_session_id``.
        user_id: Effective user id; falls back to ``DEFAULT_USER_ID`` when
            ``None`` so the Langfuse Users page works in no-auth mode.
        assistant_id: Optional agent identifier; defaults to ``"lead-agent"``.
        model_name: Model name; emitted as ``model:<name>`` in ``langfuse_tags``.
        environment: Deployment env (e.g. ``"production"``); emitted as
            ``env:<value>`` in ``langfuse_tags``.
        variant: Active lead-agent prompt variant; emitted as ``variant:<value>``
            in ``langfuse_tags`` so traces can be filtered by prompt version.
    """
    if "langfuse" not in get_enabled_tracing_providers():
        return {}

    from deerflow.runtime.user_context import DEFAULT_USER_ID

    metadata: dict[str, Any] = {
        "langfuse_session_id": thread_id,
        "langfuse_user_id": user_id or DEFAULT_USER_ID,
        "langfuse_trace_name": assistant_id or _DEFAULT_TRACE_NAME,
    }

    tags: list[str] = []
    if environment:
        tags.append(f"env:{environment}")
    if model_name:
        tags.append(f"model:{model_name}")
    if variant:
        tags.append(f"variant:{variant}")
    if tags:
        metadata["langfuse_tags"] = tags

    return metadata


def inject_langfuse_metadata(
    config: dict,
    *,
    thread_id: str | None,
    user_id: str | None = None,
    assistant_id: str | None = None,
    model_name: str | None = None,
    environment: str | None = None,
    variant: str | None = None,
) -> None:
    """Merge trace-attribute metadata into ``config`` for the active tracers.

    Shared by the gateway worker (``runtime/runs/worker.py``) and the
    embedded client (``client.py``) so the two paths cannot drift apart.

    The ``variant`` (active lead-agent prompt version) is written
    provider-agnostically — as a ``config["metadata"]["variant"]`` key and a
    ``variant:<value>`` entry in ``config["tags"]`` (both read by LangSmith) —
    so it lands even on LangSmith-only deployments where the Langfuse block
    below no-ops. For Langfuse the same label additionally flows through
    ``langfuse_tags`` (see ``build_langfuse_trace_metadata``).

    Caller-supplied metadata wins via ``setdefault`` — an upstream value
    for e.g. ``langfuse_session_id`` set by the frontend stays untouched.
    The ``config`` dict is mutated in place.
    """
    if variant:
        provider_agnostic_metadata = dict(config.get("metadata") or {})
        provider_agnostic_metadata.setdefault("variant", variant)
        config["metadata"] = provider_agnostic_metadata

        variant_tag = f"variant:{variant}"
        existing_tags = list(config.get("tags") or [])
        if variant_tag not in existing_tags:
            config["tags"] = [*existing_tags, variant_tag]

    langfuse_metadata = build_langfuse_trace_metadata(
        thread_id=thread_id,
        user_id=user_id,
        assistant_id=assistant_id,
        model_name=model_name,
        environment=environment,
        variant=variant,
    )
    if not langfuse_metadata:
        return

    merged_metadata = dict(config.get("metadata") or {})
    for key, value in langfuse_metadata.items():
        merged_metadata.setdefault(key, value)
    config["metadata"] = merged_metadata
