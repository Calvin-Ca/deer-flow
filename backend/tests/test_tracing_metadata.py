"""Tests for deerflow.tracing.metadata.build_langfuse_trace_metadata."""

from __future__ import annotations

import pytest

from deerflow.tracing import metadata as tracing_metadata


@pytest.fixture(autouse=True)
def _clear_tracing_env(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    for name in (
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    yield
    reset_tracing_config()


def _enable_langfuse(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")


def test_returns_empty_when_langfuse_disabled(monkeypatch):
    # No env vars set → langfuse not in enabled providers.
    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t-1",
        user_id="u-1",
        assistant_id="lead-agent",
        model_name="gpt-4o",
    )
    assert result == {}


def test_session_id_maps_to_thread_id(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="thread-abc",
        user_id="user-42",
    )

    assert result["langfuse_session_id"] == "thread-abc"


def test_user_id_falls_back_to_default(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="thread-abc",
        user_id=None,
    )

    assert result["langfuse_user_id"] == "default"


def test_user_id_explicit_value_wins(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="thread-abc",
        user_id="alice@example.com",
    )

    assert result["langfuse_user_id"] == "alice@example.com"


def test_trace_name_uses_assistant_id_when_provided(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        assistant_id="custom-agent",
    )

    assert result["langfuse_trace_name"] == "custom-agent"


def test_trace_name_defaults_to_lead_agent(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        assistant_id=None,
    )

    assert result["langfuse_trace_name"] == "lead-agent"


def test_tags_include_env_and_model(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        environment="production",
        model_name="gpt-4o",
    )

    assert result["langfuse_tags"] == ["env:production", "model:gpt-4o"]


def test_tags_omitted_when_no_tag_inputs(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        user_id="u",
    )

    assert "langfuse_tags" not in result


def test_thread_id_none_still_produces_metadata(monkeypatch):
    # Stateless run paths may not have a thread_id — we still want
    # user_id / trace_name to flow through so Users page works.
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id=None,
        user_id="u-1",
    )

    assert result["langfuse_session_id"] is None
    assert result["langfuse_user_id"] == "u-1"


def test_tags_include_variant(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        environment="production",
        model_name="qwen3-8b",
        variant="v2_runbook_saturated",
    )

    assert result["langfuse_tags"] == ["env:production", "model:qwen3-8b", "variant:v2_runbook_saturated"]


def test_inject_sets_provider_agnostic_variant_when_langfuse_disabled(monkeypatch):
    # LangSmith-only path: langfuse block no-ops, but the variant must still land
    # as run metadata + a config tag so traces are filterable by prompt version.
    config: dict = {}
    tracing_metadata.inject_langfuse_metadata(
        config,
        thread_id="t",
        variant="v2_runbook_saturated",
    )

    assert config["metadata"]["variant"] == "v2_runbook_saturated"
    assert "variant:v2_runbook_saturated" in config["tags"]
    # Langfuse-reserved keys are absent because langfuse is not enabled.
    assert "langfuse_session_id" not in config["metadata"]


def test_inject_variant_does_not_duplicate_existing_tag(monkeypatch):
    config: dict = {"tags": ["variant:v2_runbook_saturated"]}
    tracing_metadata.inject_langfuse_metadata(
        config,
        thread_id="t",
        variant="v2_runbook_saturated",
    )

    assert config["tags"].count("variant:v2_runbook_saturated") == 1


def test_inject_without_variant_leaves_config_untouched(monkeypatch):
    config: dict = {}
    tracing_metadata.inject_langfuse_metadata(config, thread_id="t", variant=None)

    assert "metadata" not in config
    assert "tags" not in config


def test_resolve_active_prompt_variant_default_when_path_unset():
    from types import SimpleNamespace

    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path=None))

    assert tracing_metadata.resolve_active_prompt_variant(config) == "default"


def test_resolve_active_prompt_variant_uses_file_stem():
    from types import SimpleNamespace

    # 变体标签 = 真实可解析文件的 stem（2026-07-11 起解析不到如实降级 default，不再冒充文件名）；
    # 用版本库现役文件验证，路径解析与 cwd 无关（project_root→backend→仓库根 依次探测）。
    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path="benchmark/prompts/lead_agent_v2.md"))

    assert tracing_metadata.resolve_active_prompt_variant(config) == "lead_agent_v2"


def test_resolve_active_prompt_variant_default_when_file_missing():
    from types import SimpleNamespace

    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path="prompts/no_such_file.md"))

    assert tracing_metadata.resolve_active_prompt_variant(config) == "default"


def test_resolve_active_prompt_variant_default_when_global_config_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no config.yaml")

    monkeypatch.setattr("deerflow.config.get_app_config", boom)

    assert tracing_metadata.resolve_active_prompt_variant(None) == "default"
