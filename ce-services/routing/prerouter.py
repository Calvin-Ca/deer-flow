"""前置路由（T-A1）—— 确定性能力分流 + 形态判定，把路由从弱模型手里夺回。

> 对应 AGENT_DEV §9.1 ① 前置路由、§9.5 T-A1；AGENT_PRD §4.1 四维分流 / §4.2 请求形态 /
> §4.3 粗分流决策表（唯一权威路由表）/ §4.0 口径归一 / EH-01/04/05。

**为什么放服务端、不交给 LLM**：同 §8.3/T-A2 原则。能力分流（这问题该谁答）与形态判定（要不要
反问/要不要拆解）都是 PRD §4.1 明确「**可前置观测**」的信号——交给 Qwen3-8B 自由判断 = 把保
≥95% 分流的命根子交给最不可靠环节。本模块用关键词/信号规则确定性裁定，纯函数、零 LLM、可单测。

**输出**（``RouteDecision``，供下游 orchestrator/agent 消费，不自己执行）：
  - ``capability``：``norm``（规范问答 FR-K） / ``cost``（组价 FR-P） / ``price``（价格取数 FR-I） /
    ``compound``（复合 EH-01，先拆解再逐子任务回路由，承 FR-X02/X03）。
  - 四维信号：``source_type``（static/dynamic）、``needs_calc``（数值计算 C-04）、
    ``needs_context``（项目上下文 FR-C）、``intent_count``（single/compound）。
  - 形态：``feature_complete``（仅 cost，EH-04）、``caliber_complete``（口径=版本，§4.0）。
  - ``clarify``：要不要反问 + 反问什么——**按 §8 块1 收窄**：cost 仅「特征缺」反问（EH-04），
    版本缺**不反问**（默认深圳2013/2024）；norm 缺口径走「口径反问」（EH-05 会话粘性，软度在下游）。

**与 T-A2 的关系**：本层先定 capability=norm，再由 ``standard_router`` 定具体哪部 GB——两层串联，
本层不重复做规范映射。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from norm.standard_router import family_version_of

# ── 能力分流关键词 ──
# 组价意图（FR-P）：要「套码/套定额/组价」一个构件。
COST_INTENT_KW: tuple[str, ...] = (
    "组价", "套定额", "套价", "套清单", "套什么定额", "套什么清单", "套哪个定额",
    "清单码", "清单编码", "清单编号", "选码", "选清单", "子目", "套子目", "定额子目",
)
# 价格取数意图（FR-I，动态来源）：问某材料的「当期价/趋势」。
PRICE_INTENT_KW: tuple[str, ...] = (
    "信息价", "市场价", "当期价", "当月价", "本月价", "价格趋势", "价差", "行情",
    "多少钱", "单价是多少", "价格是多少", "什么价", "报价是多少",
)
# 动态来源时间信号（配合材料 → 价格取数）。
DYNAMIC_TIME_KW: tuple[str, ...] = ("本月", "当月", "当期", "近三个月", "近几个月", "近期", "最新")
# 复合/高阶（EH-01 / FR-X02 比选 / FR-X03 结算变更）：天然跨多落点，先拆解。
COMPOUND_KW: tuple[str, ...] = (
    "比选", "方案对比", "方案比选", "哪种更省", "哪个更划算", "造价指标",
    "结算", "变更", "签证", "索赔", "结算审核",
)
# 比选意图的**柔性变体**（治漏判机制一）：中缀「做法/方案」会隔断关键词精确子串匹配
# （「哪种做法更省」不含子串「哪种更省」），关键词表穷举不尽，补一条正则兜「哪[种个样]…更省/划算」。
_COMPARE_RE = re.compile(r"哪[种个样].{0,6}(更省|更划算|更便宜|更经济|省钱|划算)")
# 项目上下文（FR-C）：引用「这份清单/本项目/上传的算量」。
CONTEXT_KW: tuple[str, ...] = (
    "这份清单", "本项目", "这个项目", "我的清单", "我上传", "上传的", "这份报价",
    "这张清单", "本工程", "算量结果", "我的boq", "我的BOQ",
)
# 项目上下文型核对意图（FR-C：漏项/错套/合理性/偏差）——常配 CONTEXT_KW。
CONTEXT_CHECK_KW: tuple[str, ...] = (
    "漏项", "漏了哪些", "错套", "高估冒算", "合理性", "是否合理", "偏差预警", "量价偏差",
)
# 数值计算（C-04）：量×价、合价、汇总、换算。
CALC_KW: tuple[str, ...] = (
    "合价", "汇总", "总造价", "总价是多少", "算合价", "量乘价", "工程量×", "换算为", "换算成",
)
# 规范/计量/计价口径信号（FR-K）：兜底归 norm，亦用于消歧。
NORM_KW: tuple[str, ...] = (
    "计算规则", "按什么计量", "怎么计量", "工程量怎么", "工程量如何", "计量单位", "规范",
    "条文", "条款", "项目特征", "工作内容", "适用范围", "章节说明", "包含哪些", "构成",
    "区别", "差异", "怎么描述", "如何描述",
)

# ── 形态判定：构件 / 特征槽（cost 特征完整度 EH-04，确定性粗判，精判仍走 cost/clarify.py 的 LLM）──
COMPONENT_KW: tuple[str, ...] = (
    "柱", "梁", "板", "墙", "基础", "楼梯", "砌块", "砖墙", "墙体", "过梁", "圈梁", "构造柱",
    "钢筋", "混凝土", "楼板", "屋面", "防水", "保温", "抹灰", "门窗", "幕墙", "独立基础",
)
# 关键特征槽信号：强度等级 / 现浇预制 / 规格尺寸 / 材料品种——任一出现即视作「描述有抓手」。
_GRADE_RE = re.compile(r"(C\s*\d{2,3}|M\s*\d{1,2}\b|MU\s*\d{1,2}|HRB\s*\d{3}|HPB\s*\d{3})", re.IGNORECASE)
_SIZE_RE = re.compile(r"\d+\s*(mm|厚|×|x|\*|公分|cm)", re.IGNORECASE)
FEATURE_QUALIFIER_KW: tuple[str, ...] = (
    "现浇", "预制", "矩形", "圆形", "异形", "水泥砂浆", "混合砂浆", "专用砂浆", "标准砖",
    "多孔砖", "空心砖", "加气", "实心", "复合", "标号", "强度等级", "断面",
)
# 材料名（价格取数常配；亦帮判 dynamic）。
MATERIAL_KW: tuple[str, ...] = (
    "钢筋", "混凝土", "商品混凝土", "水泥", "砂", "石", "砌块", "砖", "钢材", "电缆", "管材",
    "HRB400", "HRB500", "螺纹钢",
)


@dataclass
class RouteDecision:
    """前置路由结论（确定性、可审计；供下游消费，本层不执行）。

    字段：
        capability —— norm / cost / price / compound。
        source_type —— static / dynamic（动态=价格月度类）。
        needs_calc —— 是否含数值计算算子（C-04 红线，交计算工具）。
        needs_context —— 是否需挂项目上下文（FR-C）。
        intent_count —— single / compound（复合→先拆解）。
        feature_complete —— 仅 cost：构件关键特征是否够（EH-04）；非 cost 为 None。
        caliber_complete —— 口径（版本）是否明确（§4.0）；缺则 norm 触发 EH-05 反问。
        clarify —— None / "feature"（EH-04 反问特征，仅 cost） / "caliber"（EH-05 反问口径，仅 norm）。
        matched —— 命中信号（审计）。reasons —— 人类可读判定理由。
    """

    capability: str
    source_type: str
    needs_calc: bool
    needs_context: bool
    intent_count: str
    feature_complete: bool | None
    caliber_complete: bool
    clarify: str | None
    matched: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_meta(self) -> dict:
        return {
            "capability": self.capability,
            "source_type": self.source_type,
            "needs_calc": self.needs_calc,
            "needs_context": self.needs_context,
            "intent_count": self.intent_count,
            "feature_complete": self.feature_complete,
            "caliber_complete": self.caliber_complete,
            "clarify": self.clarify,
            "matched": self.matched,
            "reasons": self.reasons,
        }


def _hits(text: str, vocab: tuple[str, ...]) -> list[str]:
    return [kw for kw in vocab if kw in text]


def _has_component(text: str) -> bool:
    return bool(_hits(text, COMPONENT_KW))


def _has_feature_qualifier(text: str) -> bool:
    """构件描述是否带「关键特征抓手」（强度/规格/现浇预制/材料品种）。"""
    if _GRADE_RE.search(text) or _SIZE_RE.search(text):
        return True
    return bool(_hits(text, FEATURE_QUALIFIER_KW))


def route(query: str, *, has_project_context: bool | None = None) -> RouteDecision:
    """确定性前置路由：query（+ 可选「是否已挂项目上下文」覆盖）→ RouteDecision。

    参数：
        query —— 用户自然语言请求。
        has_project_context —— 调用方已知是否挂了 BOQ/算量（覆盖文本推断）；None 则只看文本。
    返回：RouteDecision。
    """
    text = query or ""
    reasons: list[str] = []

    cost_sig = _hits(text, COST_INTENT_KW)
    price_sig = _hits(text, PRICE_INTENT_KW)
    compound_sig = _hits(text, COMPOUND_KW)
    compare_hit = bool(_COMPARE_RE.search(text))
    norm_sig = _hits(text, NORM_KW)
    ctx_sig = _hits(text, CONTEXT_KW)
    ctx_check_sig = _hits(text, CONTEXT_CHECK_KW)
    calc_sig = _hits(text, CALC_KW)
    material_sig = _hits(text, MATERIAL_KW)
    time_sig = _hits(text, DYNAMIC_TIME_KW)

    # ── 项目上下文（FR-C）──
    needs_context = bool(has_project_context) if has_project_context is not None else bool(ctx_sig)
    if ctx_check_sig:
        needs_context = True  # 漏项/错套/合理性核对必依赖上传清单

    # ── 数值计算（C-04）──
    needs_calc = bool(calc_sig)

    # ── 复合检测（两路，EH-01）──
    #   ① 显式复合词：比选/结算/变更/签证…（COMPOUND_KW）+ 比选柔性变体（_COMPARE_RE）——治机制一。
    #   ② 多能力并列启发式（治机制二）：真实复合大量是「套码 + 按什么计量」「套码 + 当期价」这种
    #      **无显式复合词的多能力并列**，旧「先命中先出」阶梯会把它吞成单一、丢掉另一半诉求。故：
    #      组价**动作词**(COST_INTENT_KW) ∧ 规范，或 价格 ∧ 另一能力 → 判复合先拆解。
    #      **刻意只认组价「动作词」、不认构件名(COMPONENT_KW)**——「现浇柱怎么计量」只是构件名+规范信号，
    #      属单一规范问答，不该误升复合（金标 routing_eval 回归防误触，见 tools/prerouter_eval.py）。
    price_like = bool(price_sig or (material_sig and time_sig))
    explicit_compound = bool(compound_sig) or compare_hit
    multi_capability = (
        (bool(cost_sig) and bool(norm_sig))     # 组价动作 + 规范口径（如「套定额 + 按什么计量」）
        or (price_like and bool(cost_sig))       # 价格 + 组价
        or (price_like and bool(norm_sig))       # 价格 + 规范
    )

    # ── 能力分流（优先级：复合 > 价格 > 组价 > 上下文核对 > 规范兜底）──
    if explicit_compound or multi_capability:
        capability, source_type = "compound", "static"
        if explicit_compound:
            reasons.append(
                f"复合/高阶信号 {compound_sig or '哪…更省(柔性匹配)'} → 先拆解（EH-01/FR-X）")
        else:
            reasons.append(
                f"多能力并列（组价={cost_sig} 价格={price_like} 规范={norm_sig}）→ 判复合先拆解（EH-01）")
    elif price_like:
        capability, source_type = "price", "dynamic"
        reasons.append(f"价格取数信号 {price_sig or (material_sig + time_sig)} → 动态价格")
    elif cost_sig:
        capability, source_type = "cost", "static"
        reasons.append(f"组价意图 {cost_sig} → 组价 Agent")
    elif ctx_check_sig:
        capability, source_type = "cost", "static"
        reasons.append(f"项目核对 {ctx_check_sig} → 组价 + 上下文（FR-C）")
    else:
        capability, source_type = "norm", "static"
        reasons.append(f"无组价/价格意图{'，规范信号 ' + str(norm_sig) if norm_sig else '（默认）'} → 规范问答")

    # ── 口径完整度（§4.0）：版本是否明确（2013/2024）──
    _, version = family_version_of(text)
    caliber_complete = version is not None

    # ── 形态：特征完整度（仅 cost，EH-04）+ 反问裁定 ──
    feature_complete: bool | None = None
    clarify: str | None = None
    if capability == "cost":
        feature_complete = _has_component(text) and _has_feature_qualifier(text)
        if not feature_complete:
            clarify = "feature"  # EH-04 反问特征
            reasons.append("构件特征不足（缺强度/规格/材料）→ 反问特征（EH-04）")
        # §8 块1：cost 版本缺**不反问**，默认深圳2013/2024；仅标 caliber 状态供口径声明。
        if not caliber_complete:
            reasons.append("版本未给 → 默认深圳口径，不反问（§8 块1）")
    elif capability == "norm":
        if not caliber_complete:
            clarify = "caliber"  # EH-05 会话粘性反问口径（软度在下游）
            reasons.append("规范问答缺口径（版本）→ 反问口径（EH-05，会话内仅首次）")

    # ── 意图数量（EH-01）──
    intent_count = "compound" if capability == "compound" else "single"

    return RouteDecision(
        capability=capability, source_type=source_type, needs_calc=needs_calc,
        needs_context=needs_context, intent_count=intent_count,
        feature_complete=feature_complete, caliber_complete=caliber_complete,
        clarify=clarify,
        matched={
            "cost": cost_sig, "price": price_sig, "compound": compound_sig,
            "compare": compare_hit, "multi_capability": multi_capability,
            "norm": norm_sig, "context": ctx_sig, "context_check": ctx_check_sig,
            "calc": calc_sig, "material": material_sig, "time": time_sig,
        },
        reasons=reasons,
    )


# ─────────────────────────── 内置自测（无需服务、无需 LLM）───────────────────────────
# 运行：cd ce-services && uv run python -m routing.prerouter
_SELFTEST_CASES: tuple[tuple[str, str, str | None], ...] = (
    # (query, 期望 capability, 期望 clarify)
    # ── norm（规范问答）──
    ("矩形柱按什么规则计量？", "norm", "caliber"),               # 缺版本→口径反问
    ("满堂脚手架工程量怎么计算？", "norm", "caliber"),
    ("现浇混凝土柱的项目特征应该怎么描述？", "norm", "caliber"),    # 有构件词但意图是「怎么描述」=norm
    ("按 gb50500-2024 综合单价包含哪些费用？", "norm", None),     # 版本明确→不反问
    ("按 gb50856-2024 通风管道的防火阀怎么计量？", "norm", None),
    ("房建计量规范 2013 和 2024 墙面抹灰计量有什么区别？", "norm", None),
    # ── cost（组价）──
    ("C30现浇混凝土矩形柱怎么组价？", "cost", None),              # 特征全→不反问（版本缺也不问）
    ("这个柱子套什么清单码？", "cost", "feature"),               # 缺强度/材料→反问特征
    ('按 2024 国标给"MU10标准砖240厚实心砖墙M5水泥砂浆"组价', "cost", None),
    ('给"C30现浇混凝土独立基础"组价', "cost", None),
    ("按 2013 国标给\"实心砖墙\"组价", "cost", None),           # 实心砖墙=有材料抓手→特征够
    # ── price（价格取数）──
    ("深圳本月HRB400钢筋信息价是多少？", "price", None),
    ("近三个月商品混凝土价差", "price", None),
    # ── compound（复合）──
    ("这两种方案哪种更省，并给出造价指标对比", "compound", None),
    ("这个变更怎么计价、签证如何办理", "compound", None),
    # 机制一·比选柔性变体（中缀「做法」隔断精确串）→ 正则兜住
    ("现浇柱和预制柱哪种做法更省", "compound", None),
    ("这几个方案哪个更划算", "compound", None),
    # 机制二·多能力并列（无显式复合词）→ 启发式判复合
    ("C30现浇矩形柱套什么定额，并说明它按什么计量", "compound", None),   # 组价动作 + 规范
    ("这个柱子套定额，再查下HRB400钢筋当期信息价", "compound", None),     # 组价动作 + 价格
    # 反例·只有构件名 + 规范信号（无组价动作词）→ 仍是单一 norm，不误升复合
    ("现浇混凝土柱按什么规则计量", "norm", "caliber"),
)


def _selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    passed = failed = 0
    for query, exp_cap, exp_clarify in _SELFTEST_CASES:
        d = route(query)
        ok = d.capability == exp_cap and d.clarify == exp_clarify
        flag = "✓" if ok else "✗"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"{flag} cap={d.capability:<9} clarify={str(d.clarify):<8} "
              f"src={d.source_type:<7} feat={d.feature_complete} cal={d.caliber_complete}  {query[:30]}")
        if not ok:
            print(f"    期望 cap={exp_cap} clarify={exp_clarify}；matched={d.matched}")
    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {len(_SELFTEST_CASES)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
