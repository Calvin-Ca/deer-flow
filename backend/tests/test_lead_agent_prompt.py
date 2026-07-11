import threading
from types import SimpleNamespace
from typing import cast

import anyio

from deerflow.agents.lead_agent import prompt as prompt_module
from deerflow.config.app_config import AppConfig
from deerflow.config.subagents_config import CustomSubagentConfig, SubagentsAppConfig
from deerflow.skills.types import Skill, SkillCategory


def _set_skills_cache_state(*, skills=None, active=False, version=0):
    prompt_module._get_cached_skills_prompt_section.cache_clear()
    with prompt_module._enabled_skills_lock:
        prompt_module._enabled_skills_cache = skills
        prompt_module._enabled_skills_by_config_cache.clear()
        prompt_module._enabled_skills_refresh_active = active
        prompt_module._enabled_skills_refresh_version = version
        prompt_module._enabled_skills_refresh_event.clear()


def test_build_self_update_section_empty_for_default_agent():
    assert prompt_module._build_self_update_section(None) == ""


def test_apply_prompt_template_uses_magent_as_default_identity(monkeypatch):
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        lead_agent=SimpleNamespace(system_prompt_path=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr(prompt_module, "_get_enabled_skills", lambda: [])
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template()

    assert "你是MAgent，建设工程造价领域的入口 agent" in prompt
    assert "核心能力只有两类" in prompt
    assert "DeerFlow 2.0" not in prompt


def test_build_self_update_section_present_for_custom_agent():
    section = prompt_module._build_self_update_section("my-agent")

    assert "<self_update>" in section
    assert "my-agent" in section
    assert "update_agent" in section


def test_build_custom_mounts_section_returns_empty_when_no_mounts(monkeypatch):
    config = SimpleNamespace(sandbox=SimpleNamespace(mounts=[]))
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)

    assert prompt_module._build_custom_mounts_section() == ""


def test_build_custom_mounts_section_lists_configured_mounts(monkeypatch):
    mounts = [
        SimpleNamespace(container_path="/home/user/shared", read_only=False),
        SimpleNamespace(container_path="/mnt/reference", read_only=True),
    ]
    config = SimpleNamespace(sandbox=SimpleNamespace(mounts=mounts))
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)

    section = prompt_module._build_custom_mounts_section()

    assert "**Custom Mounted Directories:**" in section
    assert "`/home/user/shared`" in section
    assert "read-write" in section
    assert "`/mnt/reference`" in section
    assert "read-only" in section


def test_build_custom_mounts_section_uses_explicit_app_config_without_global_read(monkeypatch):
    mounts = [SimpleNamespace(container_path="/home/user/shared", read_only=False)]
    config = SimpleNamespace(sandbox=SimpleNamespace(mounts=mounts))

    def fail_get_app_config():
        raise AssertionError("ambient get_app_config() must not be used when app_config is explicit")

    monkeypatch.setattr("deerflow.config.get_app_config", fail_get_app_config)

    section = prompt_module._build_custom_mounts_section(app_config=config)

    assert "`/home/user/shared`" in section
    assert "read-write" in section


def test_apply_prompt_template_includes_custom_mounts(monkeypatch):
    mounts = [SimpleNamespace(container_path="/home/user/shared", read_only=False)]
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=mounts),
        skills=SimpleNamespace(container_path="/mnt/skills"),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr(prompt_module, "_get_enabled_skills", lambda: [])
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_get_memory_context", lambda agent_name=None, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template()

    assert "`/home/user/shared`" in prompt
    assert "Custom Mounted Directories" in prompt


def test_apply_prompt_template_includes_relative_path_guidance(monkeypatch):
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        skills=SimpleNamespace(container_path="/mnt/skills"),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr(prompt_module, "_get_enabled_skills", lambda: [])
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_get_memory_context", lambda agent_name=None, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template()

    assert "Treat `/mnt/user-data/workspace` as your default current working directory" in prompt
    assert "`hello.txt`, `../uploads/data.csv`, and `../outputs/report.md`" in prompt


def test_apply_prompt_template_threads_explicit_app_config_without_global_config(monkeypatch):
    mounts = [SimpleNamespace(container_path="/home/user/shared", read_only=False)]
    explicit_config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=mounts),
        skills=SimpleNamespace(container_path="/mnt/explicit-skills"),
        skill_evolution=SimpleNamespace(enabled=False),
        tool_search=SimpleNamespace(enabled=False),
        memory=SimpleNamespace(enabled=False, injection_enabled=True, max_injection_tokens=2000),
        acp_agents={},
    )

    def fail_get_app_config():
        raise AssertionError("ambient get_app_config() must not be used when app_config is explicit")

    def fail_get_memory_config():
        raise AssertionError("ambient get_memory_config() must not be used when app_config is explicit")

    monkeypatch.setattr("deerflow.config.get_app_config", fail_get_app_config)
    monkeypatch.setattr("deerflow.config.memory_config.get_memory_config", fail_get_memory_config)
    monkeypatch.setattr(prompt_module, "get_or_new_skill_storage", lambda app_config=None: SimpleNamespace(load_skills=lambda enabled_only=True: []))
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template(app_config=explicit_config)

    assert "`/home/user/shared`" in prompt
    assert "Custom Mounted Directories" in prompt


def test_apply_prompt_template_threads_explicit_app_config_to_subagents_without_global_config(monkeypatch):
    explicit_config = SimpleNamespace(
        sandbox=SimpleNamespace(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            allow_host_bash=False,
            mounts=[],
        ),
        subagents=SubagentsAppConfig(
            custom_agents={
                "researcher": CustomSubagentConfig(
                    description="Research agent\nwith details",
                    system_prompt="You research.",
                )
            }
        ),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        skill_evolution=SimpleNamespace(enabled=False),
        tool_search=SimpleNamespace(enabled=False),
        memory=SimpleNamespace(enabled=False, injection_enabled=True, max_injection_tokens=2000),
        acp_agents={},
    )

    def fail_get_app_config():
        raise AssertionError("ambient get_app_config() must not be used when app_config is explicit")

    def fail_get_subagents_app_config():
        raise AssertionError("ambient get_subagents_app_config() must not be used when app_config is explicit")

    monkeypatch.setattr("deerflow.config.get_app_config", fail_get_app_config)
    monkeypatch.setattr("deerflow.config.subagents_config.get_subagents_app_config", fail_get_subagents_app_config)
    monkeypatch.setattr(prompt_module, "get_or_new_skill_storage", lambda app_config=None: SimpleNamespace(load_skills=lambda enabled_only=True: []))
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template(subagent_enabled=True, app_config=explicit_config)

    assert "**researcher**: Research agent" in prompt
    assert "**bash**" not in prompt


def test_build_acp_section_uses_explicit_app_config_without_global_config(monkeypatch):
    explicit_config = SimpleNamespace(acp_agents={"codex": object()})

    def fail_get_acp_agents():
        raise AssertionError("ambient get_acp_agents() must not be used when app_config is explicit")

    monkeypatch.setattr("deerflow.config.acp_config.get_acp_agents", fail_get_acp_agents)

    section = prompt_module._build_acp_section(app_config=explicit_config)

    assert "ACP Agent Tasks" in section
    assert "/mnt/acp-workspace/" in section


def test_get_memory_context_uses_explicit_app_config_without_global_config(monkeypatch):
    explicit_config = SimpleNamespace(
        memory=SimpleNamespace(enabled=True, injection_enabled=True, max_injection_tokens=1234),
    )
    captured: dict[str, object] = {}

    def fail_get_memory_config():
        raise AssertionError("ambient get_memory_config() must not be used when app_config is explicit")

    def fake_get_memory_data(agent_name=None, *, user_id=None):
        captured["agent_name"] = agent_name
        captured["user_id"] = user_id
        return {"facts": []}

    def fake_format_memory_for_injection(memory_data, *, max_tokens):
        captured["memory_data"] = memory_data
        captured["max_tokens"] = max_tokens
        return "remember this"

    monkeypatch.setattr("deerflow.config.memory_config.get_memory_config", fail_get_memory_config)
    monkeypatch.setattr("deerflow.runtime.user_context.get_effective_user_id", lambda: "user-1")
    monkeypatch.setattr("deerflow.agents.memory.get_memory_data", fake_get_memory_data)
    monkeypatch.setattr("deerflow.agents.memory.format_memory_for_injection", fake_format_memory_for_injection)

    context = prompt_module._get_memory_context("agent-a", app_config=explicit_config)

    assert "<memory>" in context
    assert "remember this" in context
    assert captured == {
        "agent_name": "agent-a",
        "user_id": "user-1",
        "memory_data": {"facts": []},
        "max_tokens": 1234,
    }


def test_refresh_skills_system_prompt_cache_async_reloads_immediately(monkeypatch, tmp_path):
    def make_skill(name: str) -> Skill:
        skill_dir = tmp_path / name
        return Skill(
            name=name,
            description=f"Description for {name}",
            license="MIT",
            skill_dir=skill_dir,
            skill_file=skill_dir / "SKILL.md",
            relative_path=skill_dir.relative_to(tmp_path),
            category=SkillCategory.CUSTOM,
            enabled=True,
        )

    state = {"skills": [make_skill("first-skill")]}
    monkeypatch.setattr(prompt_module, "get_or_new_skill_storage", lambda **kwargs: __import__("types").SimpleNamespace(load_skills=lambda *, enabled_only: list(state["skills"])))
    _set_skills_cache_state()

    try:
        prompt_module.warm_enabled_skills_cache()
        assert [skill.name for skill in prompt_module._get_enabled_skills()] == ["first-skill"]

        state["skills"] = [make_skill("second-skill")]
        anyio.run(prompt_module.refresh_skills_system_prompt_cache_async)

        assert [skill.name for skill in prompt_module._get_enabled_skills()] == ["second-skill"]
    finally:
        _set_skills_cache_state()


def test_explicit_config_enabled_skills_are_cached_by_config_identity(monkeypatch, tmp_path):
    def make_skill(name: str) -> Skill:
        skill_dir = tmp_path / name
        return Skill(
            name=name,
            description=f"Description for {name}",
            license="MIT",
            skill_dir=skill_dir,
            skill_file=skill_dir / "SKILL.md",
            relative_path=skill_dir.relative_to(tmp_path),
            category=SkillCategory.CUSTOM,
            enabled=True,
        )

    config = cast(
        AppConfig,
        cast(
            object,
            SimpleNamespace(
                skills=SimpleNamespace(container_path="/mnt/skills"),
                skill_evolution=SimpleNamespace(enabled=False),
            ),
        ),
    )
    load_count = 0

    def fake_get_or_new_skill_storage(**kwargs):
        nonlocal load_count
        assert kwargs == {"app_config": config}

        def load_skills(*, enabled_only):
            nonlocal load_count
            load_count += 1
            assert enabled_only is True
            return [make_skill("cached-skill")]

        return SimpleNamespace(load_skills=load_skills)

    monkeypatch.setattr(prompt_module, "get_or_new_skill_storage", fake_get_or_new_skill_storage)
    _set_skills_cache_state()

    try:
        first = prompt_module.get_skills_prompt_section(app_config=config)
        second = prompt_module.get_skills_prompt_section(app_config=config)

        assert "cached-skill" in first
        assert "cached-skill" in second
        assert load_count == 1
    finally:
        _set_skills_cache_state()


def test_clear_cache_does_not_spawn_parallel_refresh_workers(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    active_loads = 0
    max_active_loads = 0
    call_count = 0
    lock = threading.Lock()

    def make_skill(name: str) -> Skill:
        skill_dir = tmp_path / name
        return Skill(
            name=name,
            description=f"Description for {name}",
            license="MIT",
            skill_dir=skill_dir,
            skill_file=skill_dir / "SKILL.md",
            relative_path=skill_dir.relative_to(tmp_path),
            category=SkillCategory.CUSTOM,
            enabled=True,
        )

    def fake_load_skills(enabled_only=True):
        nonlocal active_loads, max_active_loads, call_count
        with lock:
            active_loads += 1
            max_active_loads = max(max_active_loads, active_loads)
            call_count += 1
            current_call = call_count

        started.set()
        if current_call == 1:
            release.wait(timeout=5)

        with lock:
            active_loads -= 1

        return [make_skill(f"skill-{current_call}")]

    monkeypatch.setattr(prompt_module, "get_or_new_skill_storage", lambda **kwargs: __import__("types").SimpleNamespace(load_skills=lambda *, enabled_only: fake_load_skills(enabled_only=enabled_only)))
    _set_skills_cache_state()

    try:
        prompt_module.clear_skills_system_prompt_cache()
        assert started.wait(timeout=5)

        prompt_module.clear_skills_system_prompt_cache()
        release.set()
        prompt_module.warm_enabled_skills_cache()

        assert max_active_loads == 1
        assert [skill.name for skill in prompt_module._get_enabled_skills()] == ["skill-2"]
    finally:
        release.set()
        _set_skills_cache_state()


def test_resolve_system_prompt_template_defaults_when_global_config_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no config.yaml")

    monkeypatch.setattr("deerflow.config.get_app_config", boom)

    assert prompt_module._resolve_system_prompt_template(None) is prompt_module.SYSTEM_PROMPT_TEMPLATE


def test_resolve_system_prompt_template_falls_back_to_global_config(monkeypatch, tmp_path):
    override = tmp_path / "variant.txt"
    override.write_text("GLOBAL VARIANT", encoding="utf-8")
    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path=str(override)))
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)

    assert prompt_module._resolve_system_prompt_template(None) == "GLOBAL VARIANT"


def test_resolve_system_prompt_template_defaults_when_path_unset():
    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path=None))

    assert prompt_module._resolve_system_prompt_template(config) is prompt_module.SYSTEM_PROMPT_TEMPLATE


def test_resolve_system_prompt_template_reads_override_file(tmp_path):
    override = tmp_path / "variant.txt"
    override.write_text("VARIANT for {agent_name}", encoding="utf-8")
    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path=str(override)))

    assert prompt_module._resolve_system_prompt_template(config) == "VARIANT for {agent_name}"


def test_resolve_system_prompt_template_falls_back_on_missing_file(tmp_path, caplog):
    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path=str(tmp_path / "does-not-exist.txt")))

    with caplog.at_level("WARNING"):
        result = prompt_module._resolve_system_prompt_template(config)

    assert result is prompt_module.SYSTEM_PROMPT_TEMPLATE
    assert "not found under any base" in caplog.text


# ── resolve_system_prompt_file：多基座、cwd 无关解析（历史坑：cwd 依赖导致静默回退+打标说谎）──


def test_resolve_system_prompt_file_absolute_path(tmp_path):
    from deerflow.config.lead_agent_config import resolve_system_prompt_file

    f = tmp_path / "abs.md"
    f.write_text("x", encoding="utf-8")
    assert resolve_system_prompt_file(str(f)) == f.resolve()


def test_resolve_system_prompt_file_relative_resolves_against_repo_root_regardless_of_cwd(monkeypatch, tmp_path):
    """相对路径在 cwd=随便哪里 时仍能经 backend 根的上一级（仓库根）命中——根治 Path.cwd() 依赖。"""
    from deerflow.config.lead_agent_config import resolve_system_prompt_file

    monkeypatch.chdir(tmp_path)  # 故意换到无关目录
    monkeypatch.delenv("DEER_FLOW_PROJECT_ROOT", raising=False)
    resolved = resolve_system_prompt_file("benchmark/prompts/lead_agent_v1.md")
    assert resolved is not None and resolved.is_file()
    assert resolved.parts[-3:] == ("benchmark", "prompts", "lead_agent_v1.md")


def test_resolve_system_prompt_file_returns_none_when_nowhere(monkeypatch, tmp_path):
    from deerflow.config.lead_agent_config import resolve_system_prompt_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEER_FLOW_PROJECT_ROOT", raising=False)
    assert resolve_system_prompt_file("no/such/prompt.md") is None


def test_variant_label_degrades_to_default_when_file_missing(monkeypatch, tmp_path):
    """打标与加载同口径：文件解析不到时 variant 如实打 default，不再冒充文件名。"""
    from deerflow.tracing.metadata import resolve_active_prompt_variant

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEER_FLOW_PROJECT_ROOT", raising=False)
    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path="no/such/prompt.md"))
    assert resolve_active_prompt_variant(config) == "default"


def test_variant_label_uses_stem_when_file_exists():
    from deerflow.tracing.metadata import resolve_active_prompt_variant

    config = SimpleNamespace(lead_agent=SimpleNamespace(system_prompt_path="benchmark/prompts/lead_agent_v2.md"))
    assert resolve_active_prompt_variant(config) == "lead_agent_v2"


def test_apply_prompt_template_uses_override_template(monkeypatch, tmp_path):
    override = tmp_path / "variant.txt"
    override.write_text("OVERRIDE PROMPT for {agent_name}", encoding="utf-8")
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        lead_agent=SimpleNamespace(system_prompt_path=str(override)),
    )
    # Explicit app_config bypasses the _get_enabled_skills path and hits skill
    # storage directly, so stub the section builders that would otherwise read
    # real config; the test only asserts the override template is the one used.
    monkeypatch.setattr(prompt_module, "get_skills_prompt_section", lambda *args, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_get_memory_context", lambda agent_name=None, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template(app_config=config, agent_name="CostBot")

    assert prompt == "OVERRIDE PROMPT for CostBot"


def test_project_config_points_lead_agent_to_ce_prompt():
    config = AppConfig.from_file("../config.yaml")

    assert config.lead_agent.system_prompt_path == "benchmark/prompts/lead_agent_v1.md"


def test_ce_override_renders_and_carries_subagent_dispatch(monkeypatch, tmp_path):
    """真·CE override（benchmark/prompts/lead_agent_v1.md）能过 .format 渲染且含三类 subagent 调度指南。

    这条同时守两件事：① override 里只用合法占位符、字面花括号已转义——否则 apply_prompt_template
    的 .format 会在运行时 KeyError/ValueError 直接打挂 prompt（_resolve 只吞 OSError，.format 在其外）；
    ② 复合并行 / 批量 / 上下文隔离三类派子智能体的判据没被后续编辑删掉。
    """
    from pathlib import Path

    ce_prompt = Path(__file__).resolve().parent.parent.parent / "benchmark" / "prompts" / "lead_agent_v1.md"
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        lead_agent=SimpleNamespace(system_prompt_path=str(ce_prompt)),
    )
    # 只验 override 被渲染 + 花括号安全，各 section 内容与本用例无关，全部打桩为空。
    monkeypatch.setattr(prompt_module, "get_skills_prompt_section", lambda *args, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_get_memory_context", lambda agent_name=None, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    # subagent_enabled 默认 False → {subagent_section} 渲染为空串，仍完整走一遍 override 的 .format，
    # 足以暴露本文件里的坏占位符 / 未转义花括号（subagent 大段本身的花括号安全是 harness 的事，非本 override）。
    prompt = prompt_module.apply_prompt_template(app_config=config, agent_name="CostBot")

    assert "你是CostBot，建设工程造价领域的入口 agent" in prompt
    assert "<subagent_dispatch" in prompt
    # 三类场景判据各留一处锚点，防回归被删。
    assert "复合诉求并行拆分" in prompt
    assert "批量独立构件" in prompt
    assert "上下文隔离" in prompt
    # 边界红线：cost-agent 不发起有状态全流程。
    assert "无 `cost_workflow_start` 权限" in prompt


def test_ce_v2_override_renders_with_routing_table(monkeypatch):
    """v2 variant（benchmark/prompts/lead_agent_v2.md）能过 .format 渲染，查表路由与红线锚点在位。"""
    from pathlib import Path

    ce_prompt = Path(__file__).resolve().parent.parent.parent / "benchmark" / "prompts" / "lead_agent_v2.md"
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        lead_agent=SimpleNamespace(system_prompt_path=str(ce_prompt)),
    )
    monkeypatch.setattr(prompt_module, "get_skills_prompt_section", lambda *args, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_get_memory_context", lambda agent_name=None, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template(app_config=config, agent_name="CostBot")

    assert "你是CostBot" in prompt
    # 查表路由的五个动作入口（工具真名）在位。
    assert "verify_bill_code" in prompt and "cost_calc" in prompt
    assert "quota-recommend" in prompt and "cost_workflow_start" in prompt
    # 红线节在位。
    assert "<discipline" in prompt and "<clarify" in prompt


def test_default_template_is_upstream_neutral():
    """内置模板已恢复为 deerflow 上游通用版：CE 内容一律走 config 指向的 benchmark/prompts/ 版本库。

    守两件事：① 内置模板不再夹带 CE 私货（否则回退兜底时 variant=default 的语义就不干净）；
    ② 上游的 clarification 体系仍在（回退时 HITL 追问能力不丢）。
    """
    template = prompt_module.SYSTEM_PROMPT_TEMPLATE

    assert "open-source super agent" in template
    assert "<clarification_system>" in template
    # CE 专属内容不得回流内置模板。
    for ce_marker in ("cost_workflow_start", "ce-rag_match_bill_item", "<skill_runbook", "<safety_redline", "capability = cost"):
        assert ce_marker not in template


def test_apply_prompt_template_default_renders_upstream(monkeypatch):
    """默认路径（system_prompt_path 未设）渲染上游模板成功，.format 不因花括号报错。"""
    config = SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        lead_agent=SimpleNamespace(system_prompt_path=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr(prompt_module, "_get_enabled_skills", lambda: [])
    monkeypatch.setattr(prompt_module, "get_deferred_tools_prompt_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **kwargs: "")
    monkeypatch.setattr(prompt_module, "_get_memory_context", lambda agent_name=None, **kwargs: "")
    monkeypatch.setattr(prompt_module, "get_agent_soul", lambda agent_name=None: "")

    prompt = prompt_module.apply_prompt_template()

    assert "open-source super agent" in prompt
    assert "<clarification_system>" in prompt
    assert "cost_workflow_start" not in prompt


def test_warm_enabled_skills_cache_logs_on_timeout(monkeypatch, caplog):
    event = threading.Event()
    monkeypatch.setattr(prompt_module, "_ensure_enabled_skills_cache", lambda: event)

    with caplog.at_level("WARNING"):
        warmed = prompt_module.warm_enabled_skills_cache(timeout_seconds=0.01)

    assert warmed is False
    assert "Timed out waiting" in caplog.text
