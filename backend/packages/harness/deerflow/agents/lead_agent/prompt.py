from __future__ import annotations

import asyncio
import logging
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

from deerflow.config.agents_config import load_agent_soul
from deerflow.config.runtime_paths import resolve_path
from deerflow.skills.storage import get_or_new_skill_storage
from deerflow.skills.types import Skill, SkillCategory
from deerflow.subagents import get_available_subagent_names

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_ENABLED_SKILLS_REFRESH_WAIT_TIMEOUT_SECONDS = 5.0
_enabled_skills_lock = threading.Lock()
_enabled_skills_cache: list[Skill] | None = None
_enabled_skills_by_config_cache: dict[int, tuple[object, list[Skill]]] = {}
_enabled_skills_refresh_active = False
_enabled_skills_refresh_version = 0
_enabled_skills_refresh_event = threading.Event()


def _load_enabled_skills_sync() -> list[Skill]:
    return list(get_or_new_skill_storage().load_skills(enabled_only=True))


def _start_enabled_skills_refresh_thread() -> None:
    threading.Thread(
        target=_refresh_enabled_skills_cache_worker,
        name="deerflow-enabled-skills-loader",
        daemon=True,
    ).start()


def _refresh_enabled_skills_cache_worker() -> None:
    global _enabled_skills_cache, _enabled_skills_refresh_active

    while True:
        with _enabled_skills_lock:
            target_version = _enabled_skills_refresh_version

        try:
            skills = _load_enabled_skills_sync()
        except Exception:
            logger.exception("Failed to load enabled skills for prompt injection")
            skills = []

        with _enabled_skills_lock:
            if _enabled_skills_refresh_version == target_version:
                _enabled_skills_cache = skills
                _enabled_skills_refresh_active = False
                _enabled_skills_refresh_event.set()
                return

            # A newer invalidation happened while loading. Keep the worker alive
            # and loop again so the cache always converges on the latest version.
            _enabled_skills_cache = None


def _ensure_enabled_skills_cache() -> threading.Event:
    global _enabled_skills_refresh_active

    with _enabled_skills_lock:
        if _enabled_skills_cache is not None:
            _enabled_skills_refresh_event.set()
            return _enabled_skills_refresh_event
        if _enabled_skills_refresh_active:
            return _enabled_skills_refresh_event
        _enabled_skills_refresh_active = True
        _enabled_skills_refresh_event.clear()

    _start_enabled_skills_refresh_thread()
    return _enabled_skills_refresh_event


def _invalidate_enabled_skills_cache() -> threading.Event:
    global _enabled_skills_cache, _enabled_skills_refresh_active, _enabled_skills_refresh_version

    _get_cached_skills_prompt_section.cache_clear()
    with _enabled_skills_lock:
        _enabled_skills_cache = None
        _enabled_skills_by_config_cache.clear()
        _enabled_skills_refresh_version += 1
        _enabled_skills_refresh_event.clear()
        if _enabled_skills_refresh_active:
            return _enabled_skills_refresh_event
        _enabled_skills_refresh_active = True

    _start_enabled_skills_refresh_thread()
    return _enabled_skills_refresh_event


def prime_enabled_skills_cache() -> None:
    _ensure_enabled_skills_cache()


def warm_enabled_skills_cache(timeout_seconds: float = _ENABLED_SKILLS_REFRESH_WAIT_TIMEOUT_SECONDS) -> bool:
    if _ensure_enabled_skills_cache().wait(timeout=timeout_seconds):
        return True

    logger.warning("Timed out waiting %.1fs for enabled skills cache warm-up", timeout_seconds)
    return False


def _get_enabled_skills():
    return get_cached_enabled_skills()


def get_cached_enabled_skills() -> list[Skill]:
    """Return the cached enabled-skills list, kicking off a background refresh on miss.

    Safe to call from request paths: never blocks on disk I/O. Returns an empty
    list on cache miss; the next call will see the warmed result.
    """
    with _enabled_skills_lock:
        cached = _enabled_skills_cache

    if cached is not None:
        return list(cached)

    _ensure_enabled_skills_cache()
    return []


def get_enabled_skills_for_config(app_config: AppConfig | None = None) -> list[Skill]:
    """Return enabled skills using the caller's config source.

    When a concrete ``app_config`` is supplied, cache the loaded skills by that
    config object's identity so request-scoped config injection still resolves
    skill paths from the matching config without rescanning storage on every
    agent factory call.
    """
    if app_config is None:
        return _get_enabled_skills()

    cache_key = id(app_config)
    with _enabled_skills_lock:
        cached = _enabled_skills_by_config_cache.get(cache_key)
        if cached is not None:
            cached_config, cached_skills = cached
            if cached_config is app_config:
                return list(cached_skills)

    skills = list(get_or_new_skill_storage(app_config=app_config).load_skills(enabled_only=True))
    with _enabled_skills_lock:
        _enabled_skills_by_config_cache[cache_key] = (app_config, skills)
    return list(skills)


def _skill_mutability_label(category: SkillCategory | str) -> str:
    return "[自定义，可编辑]" if category == SkillCategory.CUSTOM else "[内置]"


def clear_skills_system_prompt_cache() -> None:
    _invalidate_enabled_skills_cache()


async def refresh_skills_system_prompt_cache_async() -> None:
    await asyncio.to_thread(_invalidate_enabled_skills_cache().wait)


def _build_skill_evolution_section(skill_evolution_enabled: bool) -> str:
    if not skill_evolution_enabled:
        return ""
    return """
## Skill Self-Evolution
After completing a task, consider creating or updating a skill when:
- The task required 5+ tool calls to resolve
- You overcame non-obvious errors or pitfalls
- The user corrected your approach and the corrected version worked
- You discovered a non-trivial, recurring workflow
If you used a skill and encountered issues not covered by it, patch it immediately.
Prefer patch over edit. Before creating a new skill, confirm with the user first.
Skip simple one-off tasks.
"""


def _build_available_subagents_description(available_names: list[str], bash_available: bool, *, app_config: AppConfig | None = None) -> str:
    """Dynamically build subagent type descriptions from registry.

    Mirrors Codex's pattern where agent_type_description is dynamically generated
    from all registered roles, so the LLM knows about every available type.
    """
    # Built-in descriptions (kept for backward compatibility with existing prompt quality)
    builtin_descriptions = {
        "general-purpose": "For ANY non-trivial task - web research, code exploration, file operations, analysis, etc.",
        "bash": (
            "For command execution (git, build, test, deploy operations)" if bash_available else "Not available in the current sandbox configuration. Use direct file/web tools or switch to AioSandboxProvider for isolated shell access."
        ),
    }

    # Lazy import moved outside loop to avoid repeated import overhead
    from deerflow.subagents.registry import get_subagent_config

    lines = []
    for name in available_names:
        if name in builtin_descriptions:
            lines.append(f"- **{name}**: {builtin_descriptions[name]}")
        else:
            config = get_subagent_config(name, app_config=app_config)
            if config is not None:
                desc = config.description.split("\n")[0].strip()  # First line only for brevity
                lines.append(f"- **{name}**: {desc}")

    return "\n".join(lines)


def _build_subagent_section(max_concurrent: int, *, app_config: AppConfig | None = None) -> str:
    """Build the subagent system prompt section with dynamic concurrency limit.

    Args:
        max_concurrent: Maximum number of concurrent subagent calls allowed per response.

    Returns:
        Formatted subagent section string.
    """
    n = max_concurrent
    available_names = get_available_subagent_names(app_config=app_config) if app_config is not None else get_available_subagent_names()
    bash_available = "bash" in available_names

    # Dynamically build subagent type descriptions from registry (aligned with Codex's
    # agent_type_description pattern where all registered roles are listed in the tool spec).
    available_subagents = _build_available_subagents_description(available_names, bash_available, app_config=app_config)
    direct_tool_examples = "bash, ls, read_file, web_search, etc." if bash_available else "ls, read_file, web_search, etc."
    direct_execution_example = (
        '# User asks: "Run the tests"\n# Thinking: Cannot decompose into parallel sub-tasks\n# → Execute directly\n\nbash("npm test")  # Direct execution, not task()'
        if bash_available
        else '# User asks: "Read the README"\n# Thinking: Single straightforward file read\n# → Execute directly\n\nread_file("/mnt/user-data/workspace/README.md")  # Direct execution, not task()'
    )
    return f"""<subagent_system>
**🚀 SUBAGENT MODE ACTIVE - DECOMPOSE, DELEGATE, SYNTHESIZE**

You are running with subagent capabilities enabled. Your role is to be a **task orchestrator**:
1. **DECOMPOSE**: Break complex tasks into parallel sub-tasks
2. **DELEGATE**: Launch multiple subagents simultaneously using parallel `task` calls
3. **SYNTHESIZE**: Collect and integrate results into a coherent answer

**CORE PRINCIPLE: Complex tasks should be decomposed and distributed across multiple subagents for parallel execution.**

**⛔ HARD CONCURRENCY LIMIT: MAXIMUM {n} `task` CALLS PER RESPONSE. THIS IS NOT OPTIONAL.**
- Each response, you may include **at most {n}** `task` tool calls. Any excess calls are **silently discarded** by the system — you will lose that work.
- **Before launching subagents, you MUST count your sub-tasks in your thinking:**
  - If count ≤ {n}: Launch all in this response.
  - If count > {n}: **Pick the {n} most important/foundational sub-tasks for this turn.** Save the rest for the next turn.
- **Multi-batch execution** (for >{n} sub-tasks):
  - Turn 1: Launch sub-tasks 1-{n} in parallel → wait for results
  - Turn 2: Launch next batch in parallel → wait for results
  - ... continue until all sub-tasks are complete
  - Final turn: Synthesize ALL results into a coherent answer
- **Example thinking pattern**: "I identified 6 sub-tasks. Since the limit is {n} per turn, I will launch the first {n} now, and the rest in the next turn."

**Available Subagents:**
{available_subagents}

**Your Orchestration Strategy:**

✅ **DECOMPOSE + PARALLEL EXECUTION (Preferred Approach):**

For complex queries, break them down into focused sub-tasks and execute in parallel batches (max {n} per turn):

**Example 1: "Why is Tencent's stock price declining?" (3 sub-tasks → 1 batch)**
→ Turn 1: Launch 3 subagents in parallel:
- Subagent 1: Recent financial reports, earnings data, and revenue trends
- Subagent 2: Negative news, controversies, and regulatory issues
- Subagent 3: Industry trends, competitor performance, and market sentiment
→ Turn 2: Synthesize results

**Example 2: "Compare 5 cloud providers" (5 sub-tasks → multi-batch)**
→ Turn 1: Launch {n} subagents in parallel (first batch)
→ Turn 2: Launch remaining subagents in parallel
→ Final turn: Synthesize ALL results into comprehensive comparison

**Example 3: "Refactor the authentication system"**
→ Turn 1: Launch 3 subagents in parallel:
- Subagent 1: Analyze current auth implementation and technical debt
- Subagent 2: Research best practices and security patterns
- Subagent 3: Review related tests, documentation, and vulnerabilities
→ Turn 2: Synthesize results

✅ **USE Parallel Subagents (max {n} per turn) when:**
- **Complex research questions**: Requires multiple information sources or perspectives
- **Multi-aspect analysis**: Task has several independent dimensions to explore
- **Large codebases**: Need to analyze different parts simultaneously
- **Comprehensive investigations**: Questions requiring thorough coverage from multiple angles

❌ **DO NOT use subagents (execute directly) when:**
- **Task cannot be decomposed**: If you can't break it into 2+ meaningful parallel sub-tasks, execute directly
- **Ultra-simple actions**: Read one file, quick edits, single commands
- **Need immediate clarification**: Must ask user before proceeding
- **Meta conversation**: Questions about conversation history
- **Sequential dependencies**: Each step depends on previous results (do steps yourself sequentially)

**CRITICAL WORKFLOW** (STRICTLY follow this before EVERY action):
1. **COUNT**: In your thinking, list all sub-tasks and count them explicitly: "I have N sub-tasks"
2. **PLAN BATCHES**: If N > {n}, explicitly plan which sub-tasks go in which batch:
   - "Batch 1 (this turn): first {n} sub-tasks"
   - "Batch 2 (next turn): next batch of sub-tasks"
3. **EXECUTE**: Launch ONLY the current batch (max {n} `task` calls). Do NOT launch sub-tasks from future batches.
4. **REPEAT**: After results return, launch the next batch. Continue until all batches complete.
5. **SYNTHESIZE**: After ALL batches are done, synthesize all results.
6. **Cannot decompose** → Execute directly using available tools ({direct_tool_examples})

**⛔ VIOLATION: Launching more than {n} `task` calls in a single response is a HARD ERROR. The system WILL discard excess calls and you WILL lose work. Always batch.**

**Remember: Subagents are for parallel decomposition, not for wrapping single tasks.**

**How It Works:**
- The task tool runs subagents asynchronously in the background
- The backend automatically polls for completion (you don't need to poll)
- The tool call will block until the subagent completes its work
- Once complete, the result is returned to you directly

**Usage Example 1 - Single Batch (≤{n} sub-tasks):**

```python
# User asks: "Why is Tencent's stock price declining?"
# Thinking: 3 sub-tasks → fits in 1 batch

# Turn 1: Launch 3 subagents in parallel
task(description="Tencent financial data", prompt="...", subagent_type="general-purpose")
task(description="Tencent news & regulation", prompt="...", subagent_type="general-purpose")
task(description="Industry & market trends", prompt="...", subagent_type="general-purpose")
# All 3 run in parallel → synthesize results
```

**Usage Example 2 - Multiple Batches (>{n} sub-tasks):**

```python
# User asks: "Compare AWS, Azure, GCP, Alibaba Cloud, and Oracle Cloud"
# Thinking: 5 sub-tasks → need multiple batches (max {n} per batch)

# Turn 1: Launch first batch of {n}
task(description="AWS analysis", prompt="...", subagent_type="general-purpose")
task(description="Azure analysis", prompt="...", subagent_type="general-purpose")
task(description="GCP analysis", prompt="...", subagent_type="general-purpose")

# Turn 2: Launch remaining batch (after first batch completes)
task(description="Alibaba Cloud analysis", prompt="...", subagent_type="general-purpose")
task(description="Oracle Cloud analysis", prompt="...", subagent_type="general-purpose")

# Turn 3: Synthesize ALL results from both batches
```

**Counter-Example - Direct Execution (NO subagents):**

```python
{direct_execution_example}
```

**CRITICAL**:
- **Max {n} `task` calls per turn** - the system enforces this, excess calls are discarded
- Only use `task` when you can launch 2+ subagents in parallel
- Single task = No value from subagents = Execute directly
- For >{n} sub-tasks, use sequential batches of {n} across multiple turns
</subagent_system>"""


SYSTEM_PROMPT_TEMPLATE = """
<role>
你是{agent_name}，建设工程造价领域的入口 agent。核心能力只有两类：
① **规范知识问答**：清单规范、计量规则、计价规则、条文解释、编码含义、适用边界、版本差异。
② **智能组价**：根据构件 / 做法 / 工程量描述，完成清单项匹配、套定额、询价、计算，并在必要时触发人工确认。

你不是最终计算器，也不是清单码 / 定额 / 价格的自由裁决者。你的职责是理解用户意图、选择合适的子智能体 / skill / 窄工具或完整 workflow，并忠实转述结果。
本系统不再把规范问答、组价、询价、计算和 HITL 合并成一个大 MCP 工具。单点任务使用 `ce-rag_*` / `ce-db_*` 等窄原语；完整有状态组价交给 DeerFlow 内部 `cost_workflow_start` workflow。
</role>

{soul}
{self_update_section}

<safety_redline priority="最高">
造价计量计价国标分 2013 / 2024 两版，**同一 9 位编码在两版含义不同——版本用错 = 串库 = 给出错误的编码、条文与价格**。因此：
- 编码 / 条文 / 价格**只能来自可见工具、子智能体或 workflow 的结构化返回**；返回里没有的 9 位编码、条文号、价格，你一个字都不能写——绝不自己编造、"补全"或凭记忆"顺带"给出。
这是最容易违的红线：宁可信息少，绝不多补一个编码或条文号（真出过 agent 自行补 `010504001`、`E.4.1` 这类工具根本没返回的编码/条文的事故）。
- 返回标 `need_review` / `guard.verdict=reject` / 缺价 / "数据未就绪" 时，**如实告知"需人工复核 / 数据缺口"**，不当定稿、不补编。
- 规范 / 编码 / 价格类问题必须先走已装配的本地造价能力，不凭记忆直接给条文或编码，**严禁用联网搜索代替**。
- **组价 / 价格**问题版本缺省按深圳口径处理，你不必先反问版本；但**规范问答**缺口径时相反——会话内首次必须先
  `ask_clarification` 问清「哪个地区、哪个清单规范版本」再取数（EH-05），同会话已问过则不再问。这条「不反问」只适用组价 / 价格侧，不要扩大到规范侧。
</safety_redline>

<intents priority="高">
用户意图在入口层粗分，执行时再选择子智能体、窄工具或完整 workflow：

1. `norm_qa`：规范、条文、计量规则、计价规则、清单编码含义、适用范围、版本差异。
   典型表达："这个清单项适用于什么"、"2013 和 2024 有什么区别"、"这个工程量怎么算"。
2. `cost_pricing`：组价、报价、算综合单价、匹配清单、套定额、查材料价、计算总价、列清单。
   典型表达："帮我给 C30 矩形柱组价"、"这个套什么定额"、"算一下综合单价"。
3. `compound`：既问规范又要价格 / 组价 / 比选。
   典型表达："先判断能不能这么计量，再帮我组价"、"A 做法和 B 做法哪个更省"。
4. `followup`：追问、解释、修改或继续已有任务。
   典型表达："为什么选这个清单码"、"刚才那个定额换成 A1-123"、"按 180 元/工日重新算"、"继续"。
5. `out_of_domain`：与建设工程造价无关。

这些标签只用于你理解对话和分派：不要把不同业务能力塞给一个大工具。
</intents>

<routing priority="高">
收到用户消息后，先判断是否有 `<route_decision>`：
- 如果存在 `<route_decision>`，必须优先服从其中的 `capability` / `clarify` / `route_confidence` 字段，不要自行推翻上游上下文判定。
- 如果不存在 `<route_decision>`，才按 <intents> 粗判是否属于造价领域。

执行规则：
- `capability = norm`：规范知识问答。优先分派给 `norm-qa` 子智能体 / skill；若 `clarify=caliber`，必须先用 `ask_clarification` 问清地区和清单规范版本。回答只能基于 `ce-rag_*` 返回的条文证据。
- `capability = cost`：智能组价 / 价格 / 列清单。单点任务分派给 `cost-agent` 子智能体 / skill，或直接调用 `cost_workflow_node`；完整有状态组价调用 `cost_workflow_start`。不要自己选择清单码、定额、价格。
- `capability = both` 或复合诉求：先判断是否是完整组价 workflow；否则拆成规范问答与组价子任务，分别交给对应子智能体，再汇总已经返回的事实。不要在汇总时补新编码、条文或价格。
- `capability = out_of_domain`：不调用造价工具；只说明你的能力范围是规范知识问答和智能组价。
- 问你是谁 / 能干啥 / 闲聊 / 对话历史：直接回答，不调工具。
- 完全无从判断用户要什么（连造不造价都看不出）时，才用 `ask_clarification` 反问。
</routing>

<skill_runbook priority="高">
能力边界：
- 规范问答：用 `ce-rag_search_clause` / `ce-rag_get_clause` / `ce-rag_expand_clause_refs` / `ce-rag_retrieve_evidence` 取证据，再基于证据回答。
- 清单匹配：用 `ce-rag_match_bill_item` 召回候选；候选只是 `semantic_candidate`，不能直接当最终真值。必须在候选内选择，低置信或特征不足时停下来请求人工复核。
- 结构化取数：已知 code / quota / price key 后，用 `ce-db_bill_get`、`ce-db_quota_get`、`ce-db_price_compose`、`ce-db_price_query` 等结构化工具取数。
- 计算与汇总：LLM 不算钱。综合单价、汇总、费率、层级汇总必须由 `cost_workflow_node` 的确定性节点或 workflow 返回。
- HITL：HITL 是 workflow 的中断协议，不是聊天里的自由问答。遇到 `need_review` / `needs_human_input` / `interrupt`，只说明当前要用户确认或补充什么，不替用户选择。

返回处理（**忠实转述，逐字不加料**）：
- 子智能体或工具返回候选、引用、价格来源、缺口、置信度、provenance 时，保留这些字段语义。
- 用户追问"为什么 / 依据 / 怎么来的"时，回到证据、候选、provenance 或 workflow 状态；不要凭记忆解释清单码 / 定额 / 价格。
- 用户说"改成 / 换成 / 按...重算 / 重新用..."时，按造价 follow-up 处理；交给 `cost_workflow_resume` / `cost_workflow_node` 或确定性计算链路失效后续结果并重算。
- 工具报错（服务不可达 / 503 / 502）= 服务端问题，把错误原文转达用户，不要在沙箱里建 venv / 装包 / 拷脚本自救。
</skill_runbook>

<workflow>
- 先想清楚再动手：用户是不是造价领域请求（见 <intents> / <routing>）？是则选择 norm-qa、cost-agent、`cost_workflow_start` / `cost_workflow_node` 或窄工具。
- **单点任务走专业能力**：规范问答交 norm-qa；清单匹配 / 已知 code 取数 / 价格查询交 cost-agent 或直接调用被 skill 允许的窄工具。
- **完整组价走 workflow**：需要清单匹配、套定额、询价、计算、HITL、回退复核的完整流程，调用 `cost_workflow_start`，不在 lead_agent 里手搓步骤。
- **中间过程走节点**：用户只要求完整流程中的某一步时，调用 `cost_workflow_node` 的对应节点，例如 `bill_match`、`price_compose`、`price_query`、`quota_get`、`unit_price`、`rollup`、`check`。
- **HITL 不中断成闲聊**：workflow 或工具返回 `interrupt` 时，告诉用户当前需要确认 / 输入什么，并等待用户在页面控件或后续消息里回答；不要替用户确认候选。
- **忠实转述**：拿到返回后逐字转述证据、候选、缺口、价格来源、interrupt 要求，绝不补没有返回的编码 / 条文 / 价格（见 <safety_redline>）。
{subagent_thinking}- 想完必须给出面向用户的可见回复；思考只用于规划，不要把完整答案写进思考。
</workflow>

{skills_section}

{deferred_tools_section}

{subagent_section}

<working_directory existed="true">
- 上传文件：`/mnt/user-data/uploads`；临时工作区：`/mnt/user-data/workspace`；最终产物：`/mnt/user-data/outputs`（须用 `present_files` 呈现）。
- Treat `/mnt/user-data/workspace` as your default current working directory；写脚本或命令时优先用相对路径，例如 `hello.txt`, `../uploads/data.csv`, and `../outputs/report.md`。
- PDF / PPT / Excel / Word 上传后会有同名 *.md 转换版，可直接用 `read_file` 读取。
{acp_section}
</working_directory>

<response_style>
- 用与用户相同的语言（默认中文）；直接、简洁，避免多余铺垫与过度格式化。
- 造价结果（条文引用、工料机取数）适合用表格或结构化方式呈现，并清楚标注**引用来源与规范版本**。
- 始终输出可见回复——你的思考是内部的，思考之后必须给出实际答案。
{subagent_reminder}</response_style>
"""


def _get_memory_context(agent_name: str | None = None, *, app_config: AppConfig | None = None) -> str:
    """Get memory context for injection into system prompt.

    Args:
        agent_name: If provided, loads per-agent memory. If None, loads global memory.
        app_config: Explicit application config. When provided, memory options
            are read from this value instead of the global config singleton.

    Returns:
        Formatted memory context string wrapped in XML tags, or empty string if disabled.
    """
    try:
        from deerflow.agents.memory import format_memory_for_injection, get_memory_data
        from deerflow.runtime.user_context import get_effective_user_id

        if app_config is None:
            from deerflow.config.memory_config import get_memory_config

            config = get_memory_config()
        else:
            config = app_config.memory

        if not config.enabled or not config.injection_enabled:
            return ""

        memory_data = get_memory_data(agent_name, user_id=get_effective_user_id())
        memory_content = format_memory_for_injection(memory_data, max_tokens=config.max_injection_tokens)

        if not memory_content.strip():
            return ""

        return f"""<memory>
{memory_content}
</memory>
"""
    except Exception:
        logger.exception("Failed to load memory context")
        return ""


@lru_cache(maxsize=32)
def _get_cached_skills_prompt_section(
    skill_signature: tuple[tuple[str, str, str, str], ...],
    available_skills_key: tuple[str, ...] | None,
    container_base_path: str,
    skill_evolution_section: str,
) -> str:
    filtered = [(name, description, category, location) for name, description, category, location in skill_signature if available_skills_key is None or name in available_skills_key]
    skills_list = ""
    if filtered:
        skill_items = "\n".join(
            f"    <skill>\n        <name>{name}</name>\n        <description>{description} {_skill_mutability_label(category)}</description>\n        <location>{location}</location>\n    </skill>"
            for name, description, category, location in filtered
        )
        skills_list = f"<available_skills>\n{skill_items}\n</available_skills>"
    return f"""<skill_system>
你拥有一组「技能(skills)」，为特定任务提供经过优化的工作流。每个技能内含最佳实践、方法框架，以及指向额外资源的引用。

**渐进式加载方式：**
1. 当用户的问题匹配某个技能的适用场景时，立即用下方技能标签里的 location 路径对该技能主文件调用 `read_file`
2. 读懂该技能的工作流与指令
3. 技能文件中会引用同一目录下的其他资源
4. 仅在执行过程中确有需要时，再加载被引用的资源
5. 严格按照该技能的指令执行

**技能位置：** {container_base_path}
{skill_evolution_section}
{skills_list}

</skill_system>"""


def get_skills_prompt_section(available_skills: set[str] | None = None, *, app_config: AppConfig | None = None) -> str:
    """Generate the skills prompt section with available skills list."""
    skills = get_enabled_skills_for_config(app_config)

    if app_config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
            container_base_path = config.skills.container_path
            skill_evolution_enabled = config.skill_evolution.enabled
        except Exception:
            container_base_path = "/mnt/skills"
            skill_evolution_enabled = False
    else:
        config = app_config
        container_base_path = config.skills.container_path
        skill_evolution_enabled = config.skill_evolution.enabled

    if not skills and not skill_evolution_enabled:
        return ""

    if available_skills is not None and not any(skill.name in available_skills for skill in skills):
        return ""

    skill_signature = tuple((skill.name, skill.description, skill.category, skill.get_container_file_path(container_base_path)) for skill in skills)
    available_key = tuple(sorted(available_skills)) if available_skills is not None else None
    if not skill_signature and available_key is not None:
        return ""
    skill_evolution_section = _build_skill_evolution_section(skill_evolution_enabled)
    return _get_cached_skills_prompt_section(skill_signature, available_key, container_base_path, skill_evolution_section)


def get_agent_soul(agent_name: str | None) -> str:
    # Append SOUL.md (agent personality) if present
    soul = load_agent_soul(agent_name)
    if soul:
        return f"<soul>\n{soul}\n</soul>\n" if soul else ""
    return ""


def _build_self_update_section(agent_name: str | None) -> str:
    """Prompt block that teaches the custom agent to persist self-updates via update_agent."""
    if not agent_name:
        return ""
    return f"""<self_update>
You are running as the custom agent **{agent_name}** with a persisted SOUL.md and config.yaml.

When the user asks you to update your own description, personality, behaviour, skill set, tool groups, or default model,
you MUST persist the change with the `update_agent` tool. Do NOT use `bash`, `write_file`, or any sandbox tool to edit
SOUL.md or config.yaml — those write into a temporary sandbox/tool workspace and the changes will be lost on the next turn.

Rules:
- Always pass the FULL replacement text for `soul` (no patch semantics). Start from your current SOUL above and apply the user's edits.
- Only pass the fields that should change. Omit the others to preserve them.
- Pass `skills=[]` to disable all skills, or omit `skills` to keep the existing whitelist.
- After `update_agent` returns successfully, tell the user the change is persisted and will take effect on the next turn.
</self_update>
"""


def get_deferred_tools_prompt_section(*, app_config: AppConfig | None = None) -> str:
    """Generate <available-deferred-tools> block for the system prompt.

    Lists only deferred tool names so the agent knows what exists
    and can use tool_search to load them.
    Returns empty string when tool_search is disabled or no tools are deferred.
    """
    from deerflow.tools.builtins.tool_search import get_deferred_registry

    if app_config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
        except Exception:
            return ""
    else:
        config = app_config

    if not config.tool_search.enabled:
        return ""

    registry = get_deferred_registry()
    if not registry:
        return ""

    names = "\n".join(e.name for e in registry.entries)
    return f"<available-deferred-tools>\n{names}\n</available-deferred-tools>"


def _build_acp_section(*, app_config: AppConfig | None = None) -> str:
    """Build the ACP agent prompt section, only if ACP agents are configured."""
    if app_config is None:
        try:
            from deerflow.config.acp_config import get_acp_agents

            agents = get_acp_agents()
        except Exception:
            return ""
    else:
        agents = getattr(app_config, "acp_agents", {}) or {}

    if not agents:
        return ""

    return (
        "\n**ACP Agent Tasks (invoke_acp_agent):**\n"
        "- ACP agents (e.g. codex, claude_code) run in their own independent workspace — NOT in `/mnt/user-data/`\n"
        "- When writing prompts for ACP agents, describe the task only — do NOT reference `/mnt/user-data` paths\n"
        "- ACP agent results are accessible at `/mnt/acp-workspace/` (read-only) — use `ls`, `read_file`, or `bash cp` to retrieve output files\n"
        "- To deliver ACP output to the user: copy from `/mnt/acp-workspace/<file>` to `/mnt/user-data/outputs/<file>`, then use `present_files`"
    )


def _build_custom_mounts_section(*, app_config: AppConfig | None = None) -> str:
    """Build a prompt section for explicitly configured sandbox mounts."""
    if app_config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
        except Exception:
            logger.exception("Failed to load configured sandbox mounts for the lead-agent prompt")
            return ""
    else:
        config = app_config

    mounts = config.sandbox.mounts or []

    if not mounts:
        return ""

    lines = []
    for mount in mounts:
        access = "read-only" if mount.read_only else "read-write"
        lines.append(f"- Custom mount: `{mount.container_path}` - Host directory mapped into the sandbox ({access})")

    mounts_list = "\n".join(lines)
    return f"\n**Custom Mounted Directories:**\n{mounts_list}\n- If the user needs files outside `/mnt/user-data`, use these absolute container paths directly when they match the requested directory"


def _resolve_system_prompt_template(app_config: AppConfig | None) -> str:
    """Return the lead-agent system-prompt template, honouring a config override.

    When ``app_config.lead_agent.system_prompt_path`` is set, the template is read
    from that file (relative paths resolve against the project root); otherwise the
    built-in ``SYSTEM_PROMPT_TEMPLATE`` is used. A missing/unreadable override file
    logs a warning and falls back to the built-in default so a bad path never takes
    the agent down in production.

    The override file must only use ``{placeholders}`` that are a subset of the
    kwargs passed to ``SYSTEM_PROMPT_TEMPLATE.format`` below, or ``.format`` raises
    ``KeyError``.

    When ``app_config`` is None (e.g. the embedded ``DeerFlowClient`` path, which
    does not thread an explicit config), the global ``get_app_config()`` singleton
    is consulted so the override is honoured uniformly across the gateway and
    embedded paths.
    """
    config = app_config
    if config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
        except Exception:
            return SYSTEM_PROMPT_TEMPLATE

    lead_agent_config = getattr(config, "lead_agent", None)
    template_path = getattr(lead_agent_config, "system_prompt_path", None)
    if not template_path:
        return SYSTEM_PROMPT_TEMPLATE
    try:
        return resolve_path(template_path).read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to read lead-agent system prompt override at %s; falling back to built-in template", template_path, exc_info=True)
        return SYSTEM_PROMPT_TEMPLATE


def apply_prompt_template(
    subagent_enabled: bool = False,
    max_concurrent_subagents: int = 3,
    *,
    agent_name: str | None = None,
    available_skills: set[str] | None = None,
    app_config: AppConfig | None = None,
) -> str:
    # Include subagent section only if enabled (from runtime parameter)
    n = max_concurrent_subagents
    subagent_section = _build_subagent_section(n, app_config=app_config) if subagent_enabled else ""

    # Add subagent reminder to critical_reminders if enabled
    subagent_reminder = (
        "- **Orchestrator Mode**: You are a task orchestrator - decompose complex tasks into parallel sub-tasks. "
        f"**HARD LIMIT: max {n} `task` calls per response.** "
        f"If >{n} sub-tasks, split into sequential batches of ≤{n}. Synthesize after ALL batches complete.\n"
        if subagent_enabled
        else ""
    )

    # Add subagent thinking guidance if enabled
    subagent_thinking = (
        "- **DECOMPOSITION CHECK: Can this task be broken into 2+ parallel sub-tasks? If YES, COUNT them. "
        f"If count > {n}, you MUST plan batches of ≤{n} and only launch the FIRST batch now. "
        f"NEVER launch more than {n} `task` calls in one response.**\n"
        if subagent_enabled
        else ""
    )

    # Get skills section
    skills_section = get_skills_prompt_section(available_skills, app_config=app_config)

    # Get deferred tools section (tool_search)
    deferred_tools_section = get_deferred_tools_prompt_section(app_config=app_config)

    # Build ACP agent section only if ACP agents are configured
    acp_section = _build_acp_section(app_config=app_config)
    custom_mounts_section = _build_custom_mounts_section(app_config=app_config)
    acp_and_mounts_section = "\n".join(section for section in (acp_section, custom_mounts_section) if section)

    # Build and return the fully static system prompt.
    # Memory and current date are injected per-turn via DynamicContextMiddleware
    # as a <system-reminder> in the first HumanMessage, keeping this prompt
    # identical across users and sessions for maximum prefix-cache reuse.
    return _resolve_system_prompt_template(app_config).format(
        agent_name=agent_name or "MAgent",
        soul=get_agent_soul(agent_name),
        self_update_section=_build_self_update_section(agent_name),
        skills_section=skills_section,
        deferred_tools_section=deferred_tools_section,
        subagent_section=subagent_section,
        subagent_reminder=subagent_reminder,
        subagent_thinking=subagent_thinking,
        acp_section=acp_and_mounts_section,
    )
