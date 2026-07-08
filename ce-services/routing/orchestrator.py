"""复合编排器（T-A4）—— 复合请求：拆解 → 子任务回 ① 路由 → 派发 → 综合。

> 承 PRD EH-01 拆解、
> FR-X02 比选 / FR-X03 结算变更。

**层位**：坐在前置路由 ①（``routing/prerouter``）之上。单一意图直接派发到能力层 ②；复合意图
（EH-01）才进拆解—综合回环。**模型分层（§9.3）**：拆解 + 综合是真·推理（桶 B）→ 用 **32b**
（``ORCH_LLM``）；子任务能力执行里 norm 生成/取数走 **8b**（桶 A，延迟敏感），而 cost 选码消歧
自走 **32b**（桶 B，``select_code`` 内定，§9.3「选错最贵」）——故 ``cap_*`` 仅喂 norm 生成。

**降级安全**（同 clarify/标准漂移原则，弱模型不可靠不致命）：
  - 拆解 LLM 失败 / 输出非 list → 退化为「单任务=整条 query」。
  - 综合 LLM 失败 → 确定性拼接各子答案 + 汇集引用（不丢溯源 C-01）。
  - 单个子任务派发失败 → 记 ``status=error``、不拖垮整单（其余子任务照跑）。

**可测**：``orchestrate`` 的 ``dispatch_fn`` / ``decompose_fn`` / ``synthesize_fn`` 均可注入，
默认用真实现（调服务 + LLM），单测注入 stub 即可不依赖服务/LLM 验编排 loop 与降级路径。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import requests

from common import cost_client
from common.config import (
    COST_DEFAULT_REGION, COST_DEFAULT_SPEC,
    LLM_MODEL_ID, LLM_URL, ORCH_LLM_MODEL_ID, ORCH_LLM_URL,
)
from common.guards import GUARD_C01, GUARD_C03, VERDICT_PASS, VERDICT_REJECT, GuardReport
from common.llm import call_qwen3
from norm import pipeline
from norm.standard_router import family_version_of
from routing.intent_fallback import route_hybrid
from routing.prerouter import RouteDecision, route

logger = logging.getLogger("ce-services.orchestrator")

# 拆解上限：避免一句话被拆成过多子任务压垮下游 / 失控调用。
MAX_SUBTASKS = 5

# 期号解析（FR-I）：「2026年5月 / 2026-5 / 2026.05」→ "2026-05"；「本月/当月」等相对期不解析，
# 缺期由知识层取各资源最新期（确定性，不猜日期）。
_PERIOD_RE = re.compile(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*月?")


# ─────────────────────────── ① 拆解（32b，EH-01）───────────────────────────
_DECOMPOSE_SYSTEM = """\
你是建设工程造价的复合任务拆解器。用户的一句话可能含多个独立子任务（如「先套定额、再算合价、\
并判断是否合规」）。把它拆成**彼此独立、各自可单独路由到一个能力**的子问题。

铁律：
1. 每个子问题必须**自包含**（补全指代，如「这个柱子」→带上构件描述），能脱离原句单独理解。
2. 只拆**真正独立**的子任务；本就是单一问题就只返回 1 条（不硬拆）。
3. 不臆造原句没有的诉求；不合并不同诉求到一条。
4. 最多 5 条，按执行先后排序。

只返回合法 JSON，不输出任何 JSON 以外的文字。\
"""


def _decompose_user(query: str) -> str:
    return (
        f"复合请求：{query}\n\n"
        "请拆成独立可路由的子问题，只返回合法 JSON：\n"
        '{\n'
        '  "subtasks": [\n'
        '    {"id": "s1", "query": "自包含的子问题原文", "note": "这条想解决什么（可空）"}\n'
        '  ]\n'
        '}\n\n/no_think'
    )


def decompose(query: str, llm_url: str = ORCH_LLM_URL, model_id: str = ORCH_LLM_MODEL_ID) -> list[dict]:
    """32b 把复合请求拆成独立子任务。降级安全：失败/非法 → 单任务=整条 query。

    返回：``[{id, query, note}]``（至少 1 条；最多 MAX_SUBTASKS）。
    """
    fallback = [{"id": "s1", "query": query, "note": "拆解降级：按单任务处理"}]
    try:
        raw = call_qwen3(_DECOMPOSE_SYSTEM, _decompose_user(query), llm_url, model_id)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        logger.warning("decompose LLM 不可靠 → 降级单任务：%s", query[:40])
        return fallback

    subs = raw.get("subtasks") if isinstance(raw, dict) else None
    if not isinstance(subs, list) or not subs:
        return fallback
    out: list[dict] = []
    for i, s in enumerate(subs[:MAX_SUBTASKS]):
        if not isinstance(s, dict):
            continue
        q = str(s.get("query") or "").strip()
        if not q:
            continue
        out.append({"id": str(s.get("id") or f"s{i + 1}"), "query": q,
                    "note": str(s.get("note") or "").strip()})
    return out or fallback


# ─────────────────────────── ② 派发（子任务 → 能力层，桶 A 8b）───────────────────────────
def _spec_of(query: str) -> tuple[str, str]:
    """cost 子任务取国标版本：query 显式 2013/2024 优先，否则默认深圳·2013（§4.0/T9-1）。

    返回：``(spec, spec_source)``——spec_source ∈ {"user", "default"}，供口径声明（meta.caliber）。
    """
    _, version = family_version_of(query)
    return (version, "user") if version else (COST_DEFAULT_SPEC, "default")


def _ignite_hitl(query: str, decision: RouteDecision) -> dict[str, Any]:
    """完整组价形态（Option B）：**点火**一个可中断 HITL 组价会话，返回结构化会话信封。

    前门只点火、不编排（HITL_DESIGN §10）：起会话跑到首个闸，前端根据 ``task_id`` 读取会话状态并渲染
    交互式组价控件，用户在控件里逐闸办到总造价——全程不经弱模型。版本：query 显式 2013/2024 优先，
    否则传 None 让图 setup 归一到默认深圳·2013（留口径声明事件）。点火失败（服务/LLM 不可达）→ 降级为
    一次性 compose（不致命，用户至少拿到选码），status 标降级。
    返回：``{mode:"hitl", route, task_id, status, interrupt, ui_hint}``。
    """
    from cost import session  # 局部 import：隔离 langgraph 依赖，与 dispatch_subtask 一致
    spec, spec_source = _spec_of(query)
    try:
        res = session.start(query, spec=(spec if spec_source == "user" else None),
                            region=COST_DEFAULT_REGION)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("HITL 点火失败 → 降级一次性 compose：%s", exc)
        from cost import orchestration
        fallback = orchestration.compose(query, spec, COST_DEFAULT_REGION, spec_source=spec_source)
        return {"mode": "single", "route": decision.as_meta(),
                "result": {"capability": "cost", "query": query, "status": "ok",
                           "result": fallback, "note": f"HITL 点火失败降级一次性选码：{exc}"}}
    task_id = res.get("task_id")
    logger.info("orchestrate hitl-ignite task=%s status=%s", task_id, res.get("status"))
    interrupt = res.get("interrupt")
    return {
        "mode": "hitl",
        "route": decision.as_meta(),
        "task_id": task_id,
        "status": res.get("status"),
        "interrupt": interrupt,
        "ui_hint": {
            "kind": "inline_session",
            "start_path": "/ce-cost/session/start",
            "state_path": f"/ce-cost/session/{task_id}/state",
            "resume_path": f"/ce-cost/session/{task_id}/resume",
        },
        "note": ("HITL 组价会话已启动，页面会显示交互式组价控件。"
                 "你只需用一句话告知用户会话已开始，后续直接在页面控件里逐步完成即可。"
                 "不要转述闸、编码、条文或任何数字。"),
    }


# 域外直答文案（M1 域外出口）：路由判 out_of_domain → 顶层说明能力范围，不进检索/取数管道白跑。
_CAPABILITY_STATEMENT = (
    "我是 MAgent，一个专注于建筑成本估算的智能助手，可以帮您：\n"
    "① **规范知识问答**——计量/计价规则、条文解释（GB 50500 / GB 50854 / GB 50856，2013/2024 双版）；\n"
    "② **智能组价**——描述构件（如「C30现浇矩形柱」）→ 清单编码 → 定额与工料机价，支持完整组价到总造价（逐闸确认）。\n"
    "您这个问题看起来不在上述范围内，我无法提供专业解答。"
    "若确与造价相关而被我误判，请换个说法（点明构件、规范或材料名）再试。"
)


def _out_of_domain_envelope(query: str) -> dict[str, Any]:
    """域外直答信封（M1）：capability=out_of_domain 的统一返回——不调任何能力层。

    参数：query —— 用户原句。返回：``{capability, query, status="ok", answer}``（answer=能力范围声明）。
    """
    return {"capability": "out_of_domain", "query": query, "status": "ok",
            "answer": _CAPABILITY_STATEMENT}


def _ignite_listing(query: str, decision: RouteDecision, *,
                    extract_fn: Callable[[str], dict] | None = None,
                    start_fn: Callable[..., dict] | None = None,
                    critic_fn: Callable[[str, list], dict] | None = None) -> dict[str, Any]:
    """批量列清单点火（M2 §3.1 管线①→现有图）：抽构件 → 多构件 HITL 会话 → 结构化会话信封。

    参数：query —— 原句（v0 把整条消息当抽取原文：用户把设计说明贴在消息里）；decision —— 路由判定；
      extract_fn / start_fn —— 可注入（单测 stub，本地零 langgraph 可测）；缺省用真实现。
    返回（三分支）：
      - 抽到 ≥1 件 → ``session.start(features=[...])`` 批量点火，``{mode:"hitl", listing:{count, preview},
        task_id, interrupt, ui_hint}``；前端复用现有逐闸控件；抽到的工程量随件预供（quantity_gate 自动过）。
      - 抽到 0 件 / 抽取失败 → ``need_input`` 引导贴设计说明或补构件特征（不硬拆、不空跑 compose）。
      - 点火失败（服务/图不可达）→ 降级返回抽取结果本身（用户至少拿到构件清单草稿），status 标降级。
    版本：沿 §4.0 口径——query 显式 2013/2024 优先，否则 None 交图 setup 归一默认。
    """
    from cost import listing as listing_mod
    _extract = extract_fn or listing_mod.extract_components
    env = _extract(query)
    items = (env.get("result") or {}).get("items") or []

    if env.get("status") != "ok" or not items:
        return {"mode": "single", "route": decision.as_meta(),
                "result": {"capability": "cost", "query": query, "status": "need_input",
                           "extraction": env,
                           "note": ("未能从描述中抽取出可计价构件。请把项目特征描述/设计说明原文贴出（含构件"
                                    "名称、强度等级、材料规格），或按「构件+关键特征」逐条列出后重试。")}}

    # Critic 对抗复核（M3 管线④）：对草表提质疑，随身进会话 state 供评审表标注。
    # 信封化降级（review_extraction 自身不抛）——Critic 挂了＝status=need_review 照样带上，
    # 前端据此显示「复核未运行」而非绿灯；绝不阻塞点火主链路。
    if critic_fn is None:
        from cost.critic import review_extraction as critic_fn  # 局部 import，与本文件风格一致
    critic_env = critic_fn(query, items)

    spec, spec_source = _spec_of(query)
    features = [{k: it[k] for k in ("feature", "quantity") if k in it} for it in items]
    if start_fn is None:
        from cost import session  # 局部 import：隔离 langgraph 依赖，与 _ignite_hitl 一致
        start_fn = session.start
    try:
        res = start_fn(features=features, spec=(spec if spec_source == "user" else None),
                       region=COST_DEFAULT_REGION, critic=critic_env)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("批量列清单点火失败 → 降级返回抽取草稿：%s", exc)
        return {"mode": "single", "route": decision.as_meta(),
                "result": {"capability": "cost", "query": query, "status": "degraded",
                           "extraction": env,
                           "note": f"已抽取 {len(items)} 个构件但组价会话启动失败（{exc}）；"
                                   "以下为构件清单草稿，供人工核对后重试。"}}
    task_id = res.get("task_id")
    logger.info("orchestrate listing-ignite task=%s items=%d status=%s",
                task_id, len(items), res.get("status"))
    return {
        "mode": "hitl",
        "route": decision.as_meta(),
        "task_id": task_id,
        "status": res.get("status"),
        "interrupt": res.get("interrupt"),
        "ui_hint": {
            "kind": "inline_session",
            "start_path": "/ce-cost/session/start",
            "state_path": f"/ce-cost/session/{task_id}/state",
            "resume_path": f"/ce-cost/session/{task_id}/resume",
        },
        "listing": {"count": len(items),
                    "preview": [it["feature"] for it in items[:5]],
                    "extraction_note": env.get("note"),
                    "critic": {"status": critic_env.get("status"),
                               "findings": len((critic_env.get("result") or {})
                                               .get("findings") or [])}},
        "note": (f"已从描述中抽取 {len(items)} 个构件并启动批量组价会话，页面已内嵌交互式组价控件。"
                 "你只需用一句话、你自己的话告知用户已识别的构件数量、后续在控件里逐件确认即可，"
                 "随后立即结束回合。严禁：罗列全部构件明细、转述闸内容、编造任何编码或数字。"),
    }


def _out_of_scope_envelope(cap: str, query: str, region: str) -> dict[str, Any]:
    """EH-03 跨地域体面告知（T9-5）：组价/价格显式要他省口径 → 不取数、说清范围 + 给出路（C-03）。

    参数：cap —— cost/price；query —— 子任务原文；region —— 命中的他省/他市地名。
    返回：统一信封 ``{capability, query, status="out_of_scope", answer, meta.guard}``。
    """
    action = "组价/套定额" if cap == "cost" else "查询信息价/市场价"
    answer = (
        f"本系统的组价与价格能力仅覆盖【深圳·{COST_DEFAULT_SPEC}】口径，无法按「{region}」口径{action}"
        f"（地域口径严格分侧，非深圳数据不进入答案）。"
        f"建议渠道：{region}当地工程造价管理机构发布的定额与信息价（住建部门造价站官网），"
        f"或全国性询价平台按地区检索。若您要问的是**规范条文**类问题（计量/计价规则），"
        f"可注明地区与版本改走规范问答——支持联网兜底并带降级标注。"
    )
    report = GuardReport(verdict=VERDICT_REJECT, tier="none")
    report.add(GUARD_C03, "error", f"EH-03 跨地域出界：显式「{region}」口径，超出深圳范围 → 体面告知，不取数")
    return {"capability": cap, "query": query, "status": "out_of_scope",
            "answer": answer, "meta": {"guard": report.as_meta(),
                                       "caliber": {"declared": f"深圳·{COST_DEFAULT_SPEC}",
                                                   "requested_region": region}}}


def _dispatch_price(query: str, decision: RouteDecision) -> dict[str, Any]:
    """FR-I 价格取数派发（确定性，零 LLM）：材料名 → 知识层 /price/query 当期信息价。

    材料名取自 prerouter 命中的材料词（最长者）；期号从 query 解析（2026年5月→2026-05），
    缺期由知识层取各资源最新期。零命中 → C-03 诚实拒答（no_source 不杜撰）；多期数据 →
    附确定性价差（FR-I02，计算在代码里、不入 LLM，C-04）。
    """
    env: dict[str, Any] = {"capability": "price", "query": query}
    materials = decision.matched.get("material") or []
    if not materials:
        env.update(status="need_input",
                   note="未识别出材料/设备名称，请指明（如「HRB400 钢筋」「商品混凝土 C30」）后再查当期价")
        return env
    name = max(materials, key=len)
    m = _PERIOD_RE.search(query)
    period = f"{m.group(1)}-{int(m.group(2)):02d}" if m else None

    resp = cost_client.price_query(name, region=COST_DEFAULT_REGION, period=period)
    rows = resp.get("results", [])
    caliber = {"declared": f"{COST_DEFAULT_REGION}·信息价", "region": COST_DEFAULT_REGION,
               "period": period or "最新期", "material": name}

    if not rows:
        report = GuardReport(verdict=VERDICT_REJECT, tier="none")
        report.add(GUARD_C03, "error", f"信息价零命中：{name}（{period or '最新期'}），拒答不编价")
        env.update(status="ok",
                   answer=(f"未在深圳信息价库中查到「{name}」（期：{period or '最新期'}）的价格，"
                           f"不编造报价（no_source）。建议核对材料名称/规格写法，或查阅深圳市工程造价"
                           f"管理站发布的当期《深圳建设工程价格信息》原刊。"),
                   data={"count": 0, "results": []},
                   meta={"guard": report.as_meta(), "caliber": caliber})
        return env

    lines = [f"深圳信息价查询「{name}」（{period or '各资源最新期'}）命中 {len(rows)} 条："]
    for r in rows[:8]:
        spec_txt = f" {r['spec']}" if r.get("spec") else ""
        lines.append(f"· {r['name']}{spec_txt}｜{r.get('category', '')}｜{r['price']} 元/{r['unit']}"
                     f"｜期 {r.get('period_start', '?')}~{r.get('period_end', '?')}｜{r.get('price_type', '信息价')}")
    # FR-I02 价差（确定性，C-04 在代码不入 LLM）：同名同规格同单位、多期 → 最新两期差值。
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        by_key.setdefault((r["name"], r.get("spec"), r["unit"]), []).append(r)
    for key, group in by_key.items():
        if len(group) >= 2:
            g = sorted(group, key=lambda x: str(x.get("period_start") or ""))
            delta = float(g[-1]["price"]) - float(g[-2]["price"])
            lines.append(f"· 价差（{key[0]}）：{g[-2]['period_start']} → {g[-1]['period_start']} "
                         f"变动 {delta:+.2f} 元/{key[2]}")
    periods = {str(r.get("period_start")) for r in rows}
    if len(periods) == 1:
        lines.append(f"（库内当前仅 {next(iter(periods))} 起一期数据，趋势分析需多期入库后支持）")
    lines.append("来源：深圳信息价（月刊）入库数据，带期段时效；本回答仅供参考，不替代专业造价审核。")

    report = GuardReport(verdict=VERDICT_PASS, tier="local")
    if any(not r.get("doc_id") for r in rows):
        report.add(GUARD_C01, "warn", "部分价目行缺 doc_id 溯源")
        report.provenance_complete = False
    env.update(status="ok", answer="\n".join(lines),
               data={"count": resp.get("count", len(rows)), "results": rows},
               meta={"guard": report.as_meta(), "caliber": caliber})
    return env


def dispatch_subtask(query: str, decision: RouteDecision, *,
                     cap_llm_url: str = LLM_URL, cap_model_id: str = LLM_MODEL_ID) -> dict:
    """把单个子任务派发到其能力层并归一为统一信封。

    返回信封：``{capability, query, status, ...}``：
      - norm：``status=ok`` + ``answer`` + ``cited_clauses`` + ``meta``（走 norm.pipeline，含校验闸）。
      - cost：``status=ok`` + ``result``（orchestration.compose 全量，含 meta.caliber 口径声明）。
      - price：``status=ok`` + ``answer`` + ``data``（FR-I 确定性取数，零命中 C-03 拒答不编价）。
      - cost/price 显式他省口径（EH-03）：``status=out_of_scope`` + 体面告知（不取数）。
      - 异常：``status=error`` + ``error``（不拖垮整单）。
    """
    cap = decision.capability
    env: dict[str, Any] = {"capability": cap, "query": query}
    # EH-03 出界拦截（仅 cost/price；norm 跨口径走 EH-05→联网兜底，是合法路径不拦）。
    if decision.out_of_scope_region and cap in ("cost", "price"):
        return _out_of_scope_envelope(cap, query, decision.out_of_scope_region)
    try:
        if cap == "norm":
            res = pipeline.answer_query(query, standard_hint=None,
                                        llm_url=cap_llm_url, model_id=cap_model_id)
            env.update(status="ok", answer=res.get("answer"),
                       cited_clauses=res.get("cited_clauses", []), meta=res.get("meta", {}))
        elif cap == "cost":
            from cost import orchestration  # 局部 import：避免 routing 包在无 langgraph 环境下连带失败
            # 选码走桶 B 32b（compose 内 select_code 自定，§9.3）；cap_* 仅供 norm 生成（桶 A 8b）。
            spec, spec_source = _spec_of(query)
            res = orchestration.compose(query, spec, COST_DEFAULT_REGION, spec_source=spec_source)
            env.update(status="ok", result=res)
        elif cap == "price":
            env = _dispatch_price(query, decision)
        else:  # compound 嵌套：不递归，标记交人核（拆解应已展平）
            env.update(status="unsupported", note=f"未支持的子任务能力：{cap}")
    except (requests.RequestException, ValueError) as exc:
        env.update(status="error", error=str(exc))
        logger.warning("子任务派发失败 cap=%s err=%s", cap, exc)
    return env


# ─────────────────────────── ③ 综合（32b）───────────────────────────
_SYNTH_SYSTEM = """\
你是建设工程造价的复合结论综合器。给定用户原始复合问题，以及各子任务的检索/计算结果，\
把它们整合成一份连贯、面向造价从业者的回答。

铁律：
1. **只基于子任务结果**作答，不补充子结果中没有的事实/条文/数字；不杜撰。
2. 完整保留各子结果的来源引用（条文号/定额子目/价格期段），汇入 cited_clauses。
3. 子结果间若有冲突或缺口，在 conflicts/uncertain_aspects 中如实点出。
4. 末尾附：本回答仅供参考，不替代专业造价审核。

只返回合法 JSON，不输出任何 JSON 以外的文字。\
"""


def _synth_user(query: str, results: list[dict]) -> str:
    blocks = [f"用户原始复合问题：{query}", "", "各子任务结果："]
    for r in results:
        blocks.append(f"--- 子任务（{r.get('capability')}）：{r.get('query', '')} ---")
        blocks.append(f"状态：{r.get('status')}")
        if r.get("answer"):
            blocks.append(f"回答：{r['answer']}")
        if r.get("cited_clauses"):
            blocks.append(f"引用：{json.dumps(r['cited_clauses'], ensure_ascii=False)}")
        if r.get("result"):
            blocks.append(f"结果：{json.dumps(r['result'], ensure_ascii=False)[:1500]}")
        if r.get("note"):
            blocks.append(f"说明：{r['note']}")
        if r.get("error"):
            blocks.append(f"错误：{r['error']}")
        blocks.append("")
    blocks += [
        "请综合成一份回答，只返回合法 JSON：",
        '{\n  "answer": "整合后的回答正文（含引用、免责声明）",\n'
        '  "cited_clauses": [{"clause": "...", "standard": "...", "text": "..."}],\n'
        '  "conflicts": ["子结果间的冲突，若无则空数组"],\n'
        '  "uncertain_aspects": ["需人工核实的方面，若无则空数组"]\n}',
        "",
        "/no_think",
    ]
    return "\n".join(blocks)


def _synth_fallback(query: str, results: list[dict]) -> dict:
    """综合降级：确定性拼接子答案 + 汇集引用（不丢溯源 C-01）。"""
    parts: list[str] = []
    cited: list[dict] = []
    for r in results:
        head = f"【{r.get('capability')}｜{r.get('query', '')}】"
        if r.get("status") == "ok" and r.get("answer"):
            parts.append(f"{head}\n{r['answer']}")
        elif r.get("status") == "ok" and r.get("result"):
            parts.append(f"{head}\n（组价结果见结构化数据）")
        else:
            parts.append(f"{head}\n[{r.get('status')}] {r.get('note') or r.get('error') or ''}")
        cited.extend(r.get("cited_clauses") or [])
    parts.append("（综合降级：以下为各子任务结果的确定性拼接。本回答仅供参考，不替代专业造价审核。）")
    return {"answer": "\n\n".join(parts), "cited_clauses": cited,
            "conflicts": [], "uncertain_aspects": ["综合 LLM 不可用，已降级为确定性拼接"]}


def synthesize(query: str, results: list[dict],
               llm_url: str = ORCH_LLM_URL, model_id: str = ORCH_LLM_MODEL_ID) -> dict:
    """32b 把各子任务结果综合成一份回答。降级安全：失败 → 确定性拼接（``_synth_fallback``）。"""
    try:
        out = call_qwen3(_SYNTH_SYSTEM, _synth_user(query, results), llm_url, model_id,
                         max_tokens=4096)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        logger.warning("synthesize LLM 不可靠 → 降级确定性拼接")
        return _synth_fallback(query, results)
    if not isinstance(out, dict) or not out.get("answer"):
        return _synth_fallback(query, results)
    out.setdefault("cited_clauses", [])
    out.setdefault("conflicts", [])
    out.setdefault("uncertain_aspects", [])
    return out


# ─────────────────────────── 编排主流程 ───────────────────────────
def orchestrate(
    query: str,
    *,
    has_project_context: bool | None = None,
    use_llm_fallback: bool | None = None,
    prior_capability: str | None = None,
    cap_llm_url: str = LLM_URL,
    cap_model_id: str = LLM_MODEL_ID,
    dispatch_fn: Callable[[str, RouteDecision], dict] | None = None,
    decompose_fn: Callable[[str], list[dict]] | None = None,
    synthesize_fn: Callable[[str, list[dict]], dict] | None = None,
) -> dict:
    """复合编排：① 路由（意图混合：确定性 + 低置信 LLM 兜底）→ 单一直派 / 复合则拆解→逐子任务回①→派发→综合。

    参数：
        query —— 用户请求；has_project_context —— 是否挂 BOQ（透传 prerouter）。
        use_llm_fallback —— 顶层路由是否启用 LLM 兜底（None 读 config CE_ROUTE_LLM_FALLBACK）；
          仅作用于**顶层单句路由**（口语变体/泛词从 norm 兜底捞回），拆解后的子任务已自包含、仍走确定性。
        cap_llm_url / cap_model_id —— 能力层（桶 A 8b）模型；拆解/综合/兜底分类用 32b（在默认 fn 内）。
        dispatch_fn / decompose_fn / synthesize_fn —— 可注入（单测用 stub）；缺省用真实现。
    返回：
        单一：``{mode:"single", route, result:<信封>}``；
        复合：``{mode:"compound", route, subtasks:[{route, result}...], answer:<综合>}``。
    """
    _dispatch = dispatch_fn or (lambda q, d: dispatch_subtask(
        q, d, cap_llm_url=cap_llm_url, cap_model_id=cap_model_id))
    _decompose = decompose_fn or decompose
    _synthesize = synthesize_fn or synthesize

    # 顶层：意图混合路由（确定性强信号直配 / 低置信 32b 兜底补能力分类，红线闸仍确定性重推；
    # prior_capability 供承接句会话粘性，见 prerouter EH-05 扩展）。
    decision = route_hybrid(query, has_project_context=has_project_context,
                            use_llm_fallback=use_llm_fallback,
                            prior_capability=prior_capability)

    # 域外出口（M1）：与造价无关 → 顶层直答能力范围，不进 norm 检索管道白跑、不联网兜底。
    if decision.capability == "out_of_domain":
        logger.info("orchestrate out_of_domain：%s", query[:40])
        return {"mode": "single", "route": decision.as_meta(),
                "result": _out_of_domain_envelope(query)}

    # 单一意图：无需拆解/综合，直接派发到能力层（编排器也作统一入口）。
    if decision.capability != "compound":
        # 批量列清单（M2）：listing 信号 + 非出界 → 抽构件批量点火（比 compose_full 更具体，先判）。
        if (decision.capability == "cost" and decision.matched.get("listing")
                and not decision.out_of_scope_region):
            return _ignite_listing(query, decision)
        # Option B：完整组价形态（到总造价/逐步确认）+ 非出界 → 点火 HITL 会话（前门只点火）。
        #   出界他省优先体面告知（不点火）——交 dispatch_subtask 的 EH-03 拦截走一次性信封。
        if (decision.capability == "cost" and decision.compose_full
                and not decision.out_of_scope_region):
            return _ignite_hitl(query, decision)
        result = _dispatch(query, decision)
        logger.info("orchestrate single cap=%s status=%s", decision.capability, result.get("status"))
        return {"mode": "single", "route": decision.as_meta(), "result": result}

    # 复合意图（EH-01）：拆解 → 逐子任务回 ① 路由 + 派发 → 综合。
    subtasks = _decompose(query)
    sub_records: list[dict] = []
    dispatch_inputs: list[dict] = []
    for st in subtasks:
        sub_decision = route(st["query"], has_project_context=has_project_context)
        env = _dispatch(st["query"], sub_decision)
        sub_records.append({"subtask": st, "route": sub_decision.as_meta(), "result": env})
        dispatch_inputs.append(env)

    final = _synthesize(query, dispatch_inputs)
    logger.info("orchestrate compound subtasks=%d caps=%s",
                len(subtasks), [r["result"].get("capability") for r in sub_records])
    return {
        "mode": "compound",
        "route": decision.as_meta(),
        "subtasks": sub_records,
        "answer": final,
    }


# ─────────────────────────── 内置自测（注入 stub，无需服务/LLM）───────────────────────────
def _selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    passed = failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        print(f"{'✓' if cond else '✗'} {name}{('  ' + extra) if extra else ''}")
        if cond:
            passed += 1
        else:
            failed += 1

    # stub 派发：按 capability 造假结果，记录被派发的子 query
    dispatched: list[tuple[str, str]] = []

    def stub_dispatch(q: str, d: RouteDecision) -> dict:
        dispatched.append((d.capability, q))
        if d.capability == "norm":
            return {"capability": "norm", "query": q, "status": "ok", "answer": f"规范答[{q}]",
                    "cited_clauses": [{"clause": "5.1.1", "standard": "gb50854-2024", "text": "x"}]}
        if d.capability == "cost":
            return {"capability": "cost", "query": q, "status": "ok", "result": {"code": "010502006"}}
        return {"capability": d.capability, "query": q, "status": "unsupported", "note": "stub"}

    def stub_decompose(q: str) -> list[dict]:
        return [{"id": "s1", "query": "C30现浇矩形柱怎么组价", "note": "套价"},
                {"id": "s2", "query": "矩形柱按什么计量", "note": "计量规则"}]

    def stub_synth(q: str, results: list[dict]) -> dict:
        return {"answer": "综合：" + "；".join(r.get("answer", r.get("status")) for r in results),
                "cited_clauses": [c for r in results for c in r.get("cited_clauses", [])],
                "conflicts": [], "uncertain_aspects": []}

    # ① 单一意图（norm）：mode=single、直派、不拆解
    out = orchestrate("矩形柱按什么计量", dispatch_fn=stub_dispatch,
                      decompose_fn=stub_decompose, synthesize_fn=stub_synth)
    check("单一 norm：mode=single", out["mode"] == "single")
    check("单一 norm：route.capability=norm", out["route"]["capability"] == "norm")
    check("单一 norm：未触发拆解（dispatched 1 次）", len(dispatched) == 1)

    # ② 复合意图：mode=compound、拆解 2 子任务、各自回路由派发、综合
    dispatched.clear()
    out = orchestrate("这个变更怎么计价、并核对是否合规", dispatch_fn=stub_dispatch,
                      decompose_fn=stub_decompose, synthesize_fn=stub_synth)
    check("复合：mode=compound", out["mode"] == "compound")
    check("复合：拆出 2 子任务", len(out["subtasks"]) == 2)
    check("复合：子任务回 ① 路由（cost + norm）",
          [s["route"]["capability"] for s in out["subtasks"]] == ["cost", "norm"],
          str([s["route"]["capability"] for s in out["subtasks"]]))
    check("复合：综合答非空 + 汇集引用",
          bool(out["answer"]["answer"]) and len(out["answer"]["cited_clauses"]) == 1)

    # ④ Option B：完整组价形态 → 点火 HITL；一次性/出界他省 → 不点火（打桩 _ignite_hitl 验分支，不起真会话）
    #   经 globals() 改本模块全局（orchestrate 同模块按全局名查 _ignite_hitl），避免 -m 下 __main__ 与
    #   routing.orchestrator 两份拷贝导致打桩打空。
    ignited: list[str] = []

    def _stub_ignite(q: str, d: RouteDecision) -> dict:
        ignited.append(q)
        return {"mode": "hitl", "task_id": "stub", "route": d.as_meta()}

    _orig_ignite = globals()["_ignite_hitl"]
    globals()["_ignite_hitl"] = _stub_ignite
    try:
        o1 = orchestrate("走完整组价算到总造价：C30现浇矩形柱，按2024版", dispatch_fn=stub_dispatch,
                         decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("完整组价：mode=hitl 点火", o1.get("mode") == "hitl" and len(ignited) == 1)
        o2 = orchestrate("C30现浇矩形柱套什么清单码", dispatch_fn=stub_dispatch,
                         decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("一次性选码：不点火（mode=single）", o2.get("mode") == "single" and len(ignited) == 1)
        o3 = orchestrate("北京的柱子完整组价算总造价", dispatch_fn=stub_dispatch,
                         decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("出界他省完整组价：不点火（体面告知）", o3.get("mode") == "single" and len(ignited) == 1)
    finally:
        globals()["_ignite_hitl"] = _orig_ignite

    # ⑥ 批量列清单（M2）：listing 信号 → 抽构件 → 多件点火 / 0 件引导（打桩 _ignite_listing 验分支路由）
    listed: list[str] = []

    def _stub_listing(q: str, d: RouteDecision, **kw) -> dict:
        listed.append(q)
        return {"mode": "hitl", "task_id": "stub-listing", "route": d.as_meta(),
                "listing": {"count": 3, "preview": []}}

    _orig_listing = globals()["_ignite_listing"]
    globals()["_ignite_listing"] = _stub_listing
    try:
        o_l1 = orchestrate("根据设计说明编制工程量清单：C30独立基础、C35矩形柱", dispatch_fn=stub_dispatch,
                           decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("列清单：listing 信号 → 走批量点火分支", o_l1.get("mode") == "hitl"
              and o_l1.get("task_id") == "stub-listing" and len(listed) == 1)
        o_l2 = orchestrate("C30现浇矩形柱套什么清单码", dispatch_fn=stub_dispatch,
                           decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("单件选码：不走 listing 分支", o_l2.get("mode") == "single" and len(listed) == 1)
        o_l3 = orchestrate("按北京口径给这个项目列清单", dispatch_fn=stub_dispatch,
                           decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("出界他省列清单：不点火（体面告知优先）", o_l3.get("mode") == "single" and len(listed) == 1)
    finally:
        globals()["_ignite_listing"] = _orig_listing

    # ⑤ 域外出口（M1）：路由判 out_of_domain → 顶层直答能力范围、零派发（打桩 route_hybrid 注入域外判定）
    dispatched.clear()
    _orig_rh = globals()["route_hybrid"]
    globals()["route_hybrid"] = lambda q, **kw: route(q, capability_override="out_of_domain")
    try:
        o_ood = orchestrate("今天天气怎么样", dispatch_fn=stub_dispatch,
                            decompose_fn=stub_decompose, synthesize_fn=stub_synth)
        check("域外：mode=single 直答能力范围、不派发",
              o_ood["mode"] == "single"
              and o_ood["result"]["capability"] == "out_of_domain"
              and "规范知识问答" in o_ood["result"]["answer"]
              and "智能组价" in o_ood["result"]["answer"]
              and "深圳信息价查询" not in o_ood["result"]["answer"]
              and len(dispatched) == 0)
    finally:
        globals()["route_hybrid"] = _orig_rh

    # ③ 综合降级：确定性拼接不丢引用（综合 LLM 不可用时的兜底形态）
    fb_out = _synth_fallback("原问", [
        {"capability": "norm", "query": "q1", "status": "ok", "answer": "A1",
         "cited_clauses": [{"clause": "1.1", "standard": "gb50854-2024", "text": "t"}]},
        {"capability": "price", "query": "q2", "status": "unsupported", "note": "未接入"},
    ])
    check("综合降级：拼接含两段 + 保留引用",
          "A1" in fb_out["answer"] and len(fb_out["cited_clauses"]) == 1 and fb_out["uncertain_aspects"])

    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
