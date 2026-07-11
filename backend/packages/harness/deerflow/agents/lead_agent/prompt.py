from __future__ import annotations

import asyncio
import logging
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

from deerflow.config.agents_config import load_agent_soul
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
        "general-purpose": "通用子智能体——网页调研、代码探索、文件操作、分析等各类非平凡任务。",
        "bash": ("命令执行专用（git、构建、测试、部署类操作）" if bash_available else "当前沙箱配置下不可用。请直接使用文件/网页工具，或切换 AioSandboxProvider 获得隔离 shell。"),
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
    direct_tool_examples = "bash、ls、read_file、web_search 等" if bash_available else "ls、read_file、web_search 等"
    direct_execution_example = (
        '# 用户问："跑一下测试"\n# 思考：无法拆成并行子任务\n# → 直接执行\n\nbash("npm test")  # 直接执行，不用 task()'
        if bash_available
        else '# 用户问："读一下 README"\n# 思考：单个简单文件读取\n# → 直接执行\n\nread_file("/mnt/user-data/workspace/README.md")  # 直接执行，不用 task()'
    )
    return f"""<subagent_system>
**🚀 子智能体模式已启用——拆解、委派、汇总**

你具备子智能体调度能力，角色是**任务编排者**：
1. **拆解**：把复杂任务拆成可并行的子任务
2. **委派**：在同一轮里用并行 `task` 调用同时派出多个子智能体
3. **汇总**：收齐结果后整合成连贯的答案

**核心原则：复杂任务应拆解后分给多个子智能体并行执行。**

**⛔ 并发硬上限：每轮回复最多 {n} 个 `task` 调用，没有例外。**
- 每轮最多包含 **{n} 个** `task` 工具调用，超出的会被系统**静默丢弃**——那部分工作直接丢失。
- **派子智能体之前，必须在思考里数清子任务数量：**
  - 数量 ≤ {n}：本轮全部派出。
  - 数量 > {n}：**本轮只挑最重要/最基础的 {n} 个**，其余留到下一轮。
- **多批次执行**（子任务 > {n} 个时）：
  - 第 1 轮：并行派出子任务 1-{n} → 等结果
  - 第 2 轮：并行派出下一批 → 等结果
  - …… 直到所有子任务完成
  - 最后一轮：把全部结果汇总成连贯答案
- **思考示例**："共识别出 6 个子任务，每轮上限 {n} 个，本轮先派前 {n} 个，其余下一轮。"

**可用子智能体：**
{available_subagents}

**编排策略：**

✅ **拆解 + 并行执行（首选方式）：**

复杂问题拆成聚焦的子任务，按批并行执行（每轮最多 {n} 个）：

**示例 1："腾讯股价为什么跌？"（3 个子任务 → 1 批）**
→ 第 1 轮：并行派 3 个子智能体：
- 子智能体 1：近期财报、盈利数据、营收趋势
- 子智能体 2：负面新闻、争议事件、监管动向
- 子智能体 3：行业趋势、竞品表现、市场情绪
→ 第 2 轮：汇总结果

**示例 2："对比 5 家云厂商"（5 个子任务 → 多批）**
→ 第 1 轮：并行派 {n} 个（第一批）
→ 第 2 轮：并行派剩余的
→ 最后一轮：汇总全部结果给出完整对比

**示例 3："重构鉴权系统"**
→ 第 1 轮：并行派 3 个子智能体：
- 子智能体 1：分析现有鉴权实现与技术债
- 子智能体 2：调研最佳实践与安全模式
- 子智能体 3：梳理相关测试、文档与已知漏洞
→ 第 2 轮：汇总结果

✅ **该用并行子智能体的场景（每轮最多 {n} 个）：**
- **复杂调研问题**：需要多个信息来源或多个视角
- **多维度分析**：任务有多个互相独立的维度要展开
- **大型代码库**：需要同时分析不同部分
- **全面排查**：需要多角度覆盖的问题

❌ **不该用子智能体（直接执行）的场景：**
- **拆不开的任务**：拆不出 2 个以上有意义的并行子任务，就直接执行
- **超简单操作**：读一个文件、小改动、单条命令
- **需要先问用户**：必须先澄清再动手
- **元对话**：关于对话历史本身的问题
- **顺序依赖**：每步都依赖上一步结果（自己按序做）

**关键流程**（每次动手前严格走一遍）：
1. **数数**：在思考里列出全部子任务并明确计数："共 N 个子任务"
2. **排批**：N > {n} 时明确排批计划：
   - "第 1 批（本轮）：前 {n} 个"
   - "第 2 批（下轮）：下一批"
3. **执行**：只派当前批（最多 {n} 个 `task`），不要提前派后面批次的
4. **循环**：结果回来后派下一批，直到所有批次完成
5. **汇总**：全部批次完成后统一汇总
6. **拆不开** → 用可用工具直接执行（{direct_tool_examples}）

**⛔ 违规：单轮派出超过 {n} 个 `task` 是硬错误，系统必然丢弃超出的调用，工作必然丢失。永远分批。**

**记住：子智能体是用来并行拆解的，不是给单个任务套壳的。**

**运行机制：**
- task 工具在后台异步运行子智能体
- 后端自动轮询完成状态（你不用轮询）
- 工具调用会阻塞到子智能体完成
- 完成后结果直接返回给你

**用法示例 1——单批（子任务 ≤ {n} 个）：**

```python
# 用户问："腾讯股价为什么跌？"
# 思考：3 个子任务 → 1 批装得下

# 第 1 轮：并行派 3 个子智能体
task(description="腾讯财务数据", prompt="...", subagent_type="general-purpose")
task(description="腾讯新闻与监管", prompt="...", subagent_type="general-purpose")
task(description="行业与市场趋势", prompt="...", subagent_type="general-purpose")
# 3 个并行跑 → 汇总结果
```

**用法示例 2——多批（子任务 > {n} 个）：**

```python
# 用户问："对比 AWS、Azure、GCP、阿里云、Oracle 云"
# 思考：5 个子任务 → 要分批（每批最多 {n} 个）

# 第 1 轮：派第一批 {n} 个
task(description="AWS 分析", prompt="...", subagent_type="general-purpose")
task(description="Azure 分析", prompt="...", subagent_type="general-purpose")
task(description="GCP 分析", prompt="...", subagent_type="general-purpose")

# 第 2 轮：第一批完成后派剩余批次
task(description="阿里云分析", prompt="...", subagent_type="general-purpose")
task(description="Oracle 云分析", prompt="...", subagent_type="general-purpose")

# 第 3 轮：汇总两批全部结果
```

**反例——直接执行（不派子智能体）：**

```python
{direct_execution_example}
```

**要点**：
- **每轮最多 {n} 个 `task`**——系统强制执行，超出即丢
- 只有能并行派出 2 个以上子智能体时才用 `task`
- 单个任务 = 子智能体无增益 = 直接执行
- 子任务 > {n} 个时，按每批 {n} 个跨多轮分批
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
    from deerflow.config.lead_agent_config import resolve_system_prompt_file

    resolved = resolve_system_prompt_file(str(template_path))
    if resolved is None:
        logger.warning("Lead-agent system prompt override %s not found under any base (cwd-independent lookup); falling back to built-in template", template_path)
        return SYSTEM_PROMPT_TEMPLATE
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to read lead-agent system prompt override at %s; falling back to built-in template", resolved, exc_info=True)
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
        f"- **编排者模式**：你是任务编排者——把复杂任务拆成并行子任务。**硬上限：每轮回复最多 {n} 个 `task` 调用。**超过 {n} 个子任务时按每批 ≤{n} 个分轮派出，全部批次完成后再汇总。\n" if subagent_enabled else ""
    )

    # Add subagent thinking guidance if enabled
    subagent_thinking = (
        f"- **拆解自查：这个任务能拆成 2 个以上并行子任务吗？能就数清数量。超过 {n} 个必须按每批 ≤{n} 个排批、本轮只派第一批。任何一轮都绝不派超过 {n} 个 `task`。**\n" if subagent_enabled else ""
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
