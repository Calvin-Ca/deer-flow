"""复合编排器（T-A4，AGENT_DEV §9.1 ④）—— 复合请求：拆解 → 子任务回 ① 路由 → 派发 → 综合。

> 对应 §9.5 T-A4 / §2.2 冲突4「缺 Orchestrator」/ §3 改3「架构归位」；承 PRD EH-01 拆解、
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
    """完整组价形态（Option B）：**点火**一个可中断 HITL 组价会话，返回 task_id + cost-hitl marker。

    前门只点火、不编排（HITL_DESIGN §10）：起会话跑到首个闸，前端据 marker/task_id 内嵌渲染交互式
    组价控件，用户在控件里逐闸办到总造价——全程不经弱模型。版本：query 显式 2013/2024 优先，否则
    传 None 让图 setup 归一到默认深圳·2013（留口径声明事件）。点火失败（服务/LLM 不可达）→ 降级为
    一次性 compose（不致命，用户至少拿到选码），status 标降级。
    返回：``{mode:"hitl", route, task_id, marker, status, first_gate}``。
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
    marker = "```cost-hitl\n" + json.dumps({"task_id": task_id}, ensure_ascii=False) + "\n```"
    logger.info("orchestrate hitl-ignite task=%s status=%s", task_id, res.get("status"))
    # **刻意不返回 first_gate**：前端内嵌控件自己 getSessionState 拿闸，无需它；返回给弱模型 lead 反而
    # 被它读去用文字转述首闸（编「1/13、请选择 1确认2覆盖3补充」等，与卡片真按钮重复且误导）。只留 note
    # 明示「只回一句就停、勿转述闸」——素材少了、弱模型没得发挥（红线不靠自觉：能不给的素材就不给）。
    return {"mode": "hitl", "route": decision.as_meta(), "task_id": task_id,
            "marker": marker, "status": res.get("status"),
            "note": ("HITL 组价会话已起、前端已内嵌交互控件。请**只回一句**"
                     "「已为你起好组价会话，请在下方控件逐步确认编码/定额/价格/费率直到总造价」并停下——"
                     "勿转述闸内容、勿列操作选项、勿编造编码/条文（逐闸交互全在控件里、不经你）。")}


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
    cap_llm_url: str = LLM_URL,
    cap_model_id: str = LLM_MODEL_ID,
    dispatch_fn: Callable[[str, RouteDecision], dict] | None = None,
    decompose_fn: Callable[[str], list[dict]] | None = None,
    synthesize_fn: Callable[[str, list[dict]], dict] | None = None,
) -> dict:
    """复合编排：① 路由 → 单一直派 / 复合则拆解→逐子任务回①→派发→综合。

    参数：
        query —— 用户请求；has_project_context —— 是否挂 BOQ（透传 prerouter）。
        cap_llm_url / cap_model_id —— 能力层（桶 A 8b）模型；拆解/综合用 32b（在默认 fn 内）。
        dispatch_fn / decompose_fn / synthesize_fn —— 可注入（单测用 stub）；缺省用真实现。
    返回：
        单一：``{mode:"single", route, result:<信封>}``；
        复合：``{mode:"compound", route, subtasks:[{route, result}...], answer:<综合>}``。
    """
    _dispatch = dispatch_fn or (lambda q, d: dispatch_subtask(
        q, d, cap_llm_url=cap_llm_url, cap_model_id=cap_model_id))
    _decompose = decompose_fn or decompose
    _synthesize = synthesize_fn or synthesize

    decision = route(query, has_project_context=has_project_context)

    # 单一意图：无需拆解/综合，直接派发到能力层（编排器也作统一入口）。
    if decision.capability != "compound":
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
