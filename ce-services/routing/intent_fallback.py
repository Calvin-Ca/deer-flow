"""意图混合路由（确定性 + LLM 兜底）—— 只在确定性**低置信**时兜底补一次能力分类。

> 对应 AGENT_TODO「意图混合路由（确定性 + LLM 兜底，归 benchmark M1 路由线）」。

**为什么要 LLM 兜底**：``routing/prerouter`` 是关键词/信号确定性路由——多数流量零延迟、金标可回归，
但两处天然会漏：① **关键词穷举疲劳**（同义变体列不尽）；② **口语变体漏判**（「帮我把这根柱子弄一下」
无任何组价动作词）。这些恰是 prerouter 判 ``route_confidence="low"`` 的场景（落 norm 兜底、只命中泛词/
纯默认、无版本锁）。混合路由的分工——

  - **确定性命中强信号**（cost / price 强 / compound / 显式 GB / 强 norm 实词 / 版本锁）→ 直接走，
    ``route_confidence="high"``，**不调 LLM**（多数流量、零延迟、金标回归护栏不变）。
  - **低置信**（``route_confidence="low"``）→ 调 **32b 兜底分类器**（temp=0、enum JSON、校验），把
    可能被口语裹住的 cost/price/compound 从 norm 兜底里捞回来。

**红线不交给弱模型**（AGENT_TODO 硬约束）：LLM **只补 capability 分类**，**绝不碰安全闸**——
EH-03 跨地域出界、口径 caliber、特征 feature、compose_full 形态，全部在 ``route(capability_override=...)``
里**确定性重推**。LLM 判错顶多是能力分类偏一点（可被下游 guard/取数零命中兜住），碰不到「按错口径
给数据」这类不可逆红线。

**fail-safe**：LLM 不可达 / 输出非法 / 越界 → 一律**跌回确定性决策**（保持原 low-conf 的 norm 兜底），
不因 LLM 抖动而路由失败。纯函数 ``route`` 不引入 LLM 依赖，混合逻辑独立此模块（保 prerouter 可离线单测）。
"""
from __future__ import annotations

import logging

import requests

from common.config import ORCH_LLM_MODEL_ID, ORCH_LLM_URL, ROUTE_LLM_FALLBACK
from common.llm import call_qwen3
from routing.prerouter import VALID_CAPABILITIES, RouteDecision, route

logger = logging.getLogger("ce-services.intent_fallback")

# ── 兜底分类器提示（32b、temp=0、JSON enum；只判能力，不碰口径/特征/出界）──
_FALLBACK_SYSTEM = """\
你是建设工程造价 agent 的「意图能力分类器」。确定性关键词路由已判本句为**低置信**（没命中明确信号），\
请你判定它的**主能力落点**，只做这一件事，供下游确定性编排。

能力枚举（只能选其一）：
- norm —— 规范问答：问计量/计价规则、项目特征、工作内容、综合单价构成、条文含义、两版区别等（要「规则/解释」）。
- cost —— 组价：要给某个构件套清单/套定额/选编码/列清单/组综合单价（要「给某构件算价/选码/出清单」）。
- price —— 价格取数：问某材料/设备的信息价、市场价、当期价、价差、价格趋势（要「某材料多少钱/价怎么变」）。
- compound —— 复合：一句话里含多个独立诉求（如「先套定额再算合价并判合规」「A、B 两方案比选」）。
- out_of_domain —— 与建设工程造价领域完全无关：闲聊问候、天气、写代码、旅游、通用百科等。

判定要点：
1. 拿不准是 norm 还是 cost 时——若在**问规则/解释**归 norm，若在**要给具体构件算价/选码**归 cost。
2. 只有明确的多诉求并列才判 compound，不要硬拆单一问题。
3. **不要**判断规范版本、地区口径、特征是否齐全——那些由下游确定性处理，你只出能力标签。
4. 域内但表述含糊时**不要**判 out_of_domain（宁可归 norm 交下游检索兜底）；只有明显与造价无关才判。

只返回合法 JSON：{"capability": "norm|cost|price|compound|out_of_domain", "reason": "一句话依据"}，不输出任何 JSON 以外的文字。\
"""


def _fallback_user(query: str) -> str:
    return (
        f"用户请求：{query}\n\n"
        '判定主能力落点，只返回合法 JSON：{"capability": "norm|cost|price|compound|out_of_domain", "reason": "一句话依据"}'
        "\n\n/no_think"
    )


def classify_intent(query: str,
                    llm_url: str = ORCH_LLM_URL,
                    model_id: str = ORCH_LLM_MODEL_ID) -> str | None:
    """LLM 兜底能力分类：query → capability（∈ VALID_CAPABILITIES）或 None。

    参数：query 用户请求；llm_url/model_id 默认桶 B 32b（未部署时成对回落 8b，见 config）。
    返回：合法能力标签；**任何异常/越界一律返回 None**（fail-safe，由调用方跌回确定性决策）。
    temp=0 求稳（分类不需创造性），JSON enum 由 system prompt 钉死、代码再校验一次。
    """
    try:
        raw = call_qwen3(_FALLBACK_SYSTEM, _fallback_user(query), llm_url, model_id,
                         temperature=0.0, max_tokens=256)
    except (requests.RequestException, ValueError) as exc:  # 含 json.JSONDecodeError(ValueError 子类)
        logger.warning("意图兜底分类 LLM 不可靠 → fail-safe 跌回确定性：%s", exc)
        return None
    cap = raw.get("capability") if isinstance(raw, dict) else None
    if cap in VALID_CAPABILITIES:
        return cap
    logger.warning("意图兜底分类越界/缺字段 cap=%r → fail-safe 跌回确定性：%s", cap, query[:40])
    return None


def route_hybrid(query: str, *,
                 has_project_context: bool | None = None,
                 use_llm_fallback: bool | None = None,
                 llm_url: str = ORCH_LLM_URL,
                 model_id: str = ORCH_LLM_MODEL_ID,
                 classify_fn=None) -> RouteDecision:
    """混合路由：确定性 ``route`` + 低置信时 LLM 兜底能力分类（红线闸仍确定性重推）。

    参数：
        query / has_project_context —— 透传 ``route``。
        use_llm_fallback —— 是否启用兜底；None 则读 config ``ROUTE_LLM_FALLBACK``（可 env 关）。
        llm_url / model_id —— 兜底分类器模型（默认桶 B 32b）。
        classify_fn —— 可注入的分类函数（单测用 stub），缺省用 ``classify_intent``。
    流程：
        ① 确定性 route → 若 ``route_confidence="high"``：直接返回（多数流量，零 LLM）。
        ② 低置信且启用兜底：调分类器 → 得能力 cap。
        ③ cap 有效且**与确定性判定不同** → ``route(capability_override=cap)`` 重推（形态/红线确定性）。
           cap 为 None（fail-safe）或与原判相同 → 保持确定性决策不变。
    返回：``RouteDecision``（``route_source`` 标 deterministic / llm_fallback，供审计升级率）。
    """
    decision = route(query, has_project_context=has_project_context)

    enabled = ROUTE_LLM_FALLBACK if use_llm_fallback is None else use_llm_fallback
    if decision.route_confidence != "low" or not enabled:
        return decision

    _classify = classify_fn or classify_intent
    cap = _classify(query, llm_url, model_id) if classify_fn is None else _classify(query)
    if cap is None or cap == decision.capability:
        logger.info("意图兜底：低置信但未改判（cap=%s）→ 保持确定性 norm 兜底", cap)
        return decision

    upgraded = route(query, has_project_context=has_project_context, capability_override=cap)
    logger.info("意图兜底升级：%s → %s（route_confidence 原 low）", decision.capability, cap)
    return upgraded


# ─────────────────────────── 内置自测（注入 stub 分类器，无需服务/LLM）───────────────────────────
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

    # ① 高置信（强信号）→ 不调兜底、源保持 deterministic
    calls: list[str] = []

    def stub_never(q: str) -> str | None:
        calls.append(q)
        return "cost"

    d = route_hybrid("C30现浇混凝土矩形柱怎么组价？", use_llm_fallback=True, classify_fn=stub_never)
    check("高置信 cost：不调兜底", d.capability == "cost" and d.route_source == "deterministic"
          and len(calls) == 0)

    # ② 低置信 + 兜底判 cost（口语变体从 norm 捞回）→ 升级 + 红线闸确定性重推（缺特征→feature 反问）
    calls.clear()

    def stub_cost(q: str) -> str | None:
        calls.append(q)
        return "cost"

    d = route_hybrid("帮我把这根柱子弄一下", use_llm_fallback=True, classify_fn=stub_cost)
    check("低置信→兜底升级 cost", d.capability == "cost" and d.route_source == "llm_fallback"
          and len(calls) == 1)
    check("升级后红线闸确定性重推：缺特征→feature", d.clarify == "feature")

    # ③ 低置信 + 兜底判 price（他省口径）→ 升级 price 且 **EH-03 出界仍确定性识别**（LLM 不碰安全闸）
    #   刻意用无任何价格/组价关键词的口语句（确定性判 norm 低置信），逼真走 override 路径再验红线重推。
    q3 = "北京这个东西给我弄清楚下"
    assert route(q3).route_confidence == "low", "测例③须确定性低置信才真验 override"
    d = route_hybrid(q3, use_llm_fallback=True, classify_fn=lambda q: "price")
    check("低置信→兜底 price 且 EH-03 出界确定性识别",
          d.capability == "price" and d.route_source == "llm_fallback"
          and d.out_of_scope_region == "北京", str(d.out_of_scope_region))

    # ④ fail-safe：兜底返回 None（LLM 抖动/越界）→ 保持确定性 norm 兜底
    d = route_hybrid("这两个有什么区别", use_llm_fallback=True, classify_fn=lambda q: None)
    check("fail-safe：兜底 None → 保持确定性 norm", d.capability == "norm"
          and d.route_source == "deterministic")

    # ④b 低置信 + 兜底判 out_of_domain（域外闲聊）→ 升级域外标签（编排器据此直答能力范围，不进检索）
    d = route_hybrid("今天天气怎么样", use_llm_fallback=True, classify_fn=lambda q: "out_of_domain")
    check("低置信→兜底 out_of_domain", d.capability == "out_of_domain"
          and d.route_source == "llm_fallback" and d.clarify is None)

    # ⑤ 兜底判定与确定性同为 norm → 不改判、保持 deterministic
    d = route_hybrid("综合单价的构成是什么", use_llm_fallback=True, classify_fn=lambda q: "norm")
    check("兜底确认 norm → 不改判", d.capability == "norm" and d.route_source == "deterministic")

    # ⑥ 兜底关闭（use_llm_fallback=False）→ 低置信也不调、保持确定性
    calls.clear()
    d = route_hybrid("帮我把这根柱子弄一下", use_llm_fallback=False, classify_fn=stub_cost)
    check("兜底关闭：低置信也不调", d.capability == "norm" and len(calls) == 0)

    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
