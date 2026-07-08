"""前置路由（T-A1）—— 确定性能力分流 + 形态判定，把路由从弱模型手里夺回。

> 对应 PRD §4.1 四维分流 / §4.2 请求形态 /
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
# 列清单/列项意图（M1 补盲区）：从项目/构件描述「生成清单」的方向——区别于「套/选清单」给单构件找码。
# 批量列清单的专属落点在 M2（设计说明解析→批量组价）；当前先归 cost 能力，避免误落 norm
# 被反问规范版本（答非所问）。注意**不含裸「列项」**：「能同时列项吗」是规范问答（金标 A9），
# 裸词二义 → 只在伴随「清单/构件」时算列清单意图（见 route() 内条件判）。
LISTING_INTENT_KW: tuple[str, ...] = (
    "列清单", "清单列项", "编制清单", "清单编制", "编制工程量清单", "开清单",
)
# 柔性变体（同 _COMPARE_RE 治法）：「列一下清单 / 开个清单 / 列份清单」——中缀量词隔断精确子串。
_LISTING_RE = re.compile(r"[列开]\s*[一两]?[下个份张]?\s*(工程量)?清单")
# 价格取数意图（FR-I，动态来源）：问某材料的「当期价/趋势」。
# 分两档：**强信号**（信息价/市场价/趋势等，明确指向动态价格源）与**泛化问价**（"多少钱"等，
# 常只是组价诉求的口语尾巴）。复合判定（multi_capability）只认强信号——"这个柱子组价多少钱"
# 是单一组价诉求，不该被"多少钱"误升复合。
PRICE_STRONG_KW: tuple[str, ...] = (
    "信息价", "市场价", "当期价", "当月价", "本月价", "价格趋势", "价差", "行情",
)
PRICE_INTENT_KW: tuple[str, ...] = PRICE_STRONG_KW + (
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
# 显式规范号（如 gb50016 / GB/T 50854 / GB 50500）：点名了规范号即视作口径已明确，不再反问版本。
_EXPLICIT_GB_RE = re.compile(r"(?i)gb\s*/?\s*t?\s*[-_]?\s*5\d{4}")

# 数值计算（C-04）：量×价、合价、汇总、换算。
CALC_KW: tuple[str, ...] = (
    "合价", "汇总", "总造价", "总价是多少", "算合价", "量乘价", "工程量×", "换算为", "换算成",
)
# 完整组价形态（Option B）：走「到总造价 / 逐步确认」的完整 HITL 流程（vs 一次性选码取数）。
# 仅在 capability=cost 时判定；命中即由编排器点火 HITL 会话（前端内嵌控件逐闸办到总造价）。
COMPOSE_FULL_KW: tuple[str, ...] = (
    "完整组价", "走完整", "走流程", "组价流程", "全流程", "完整流程",
    "逐步确认", "逐项确认", "逐闸", "算到总价", "算到总造价", "到总造价",
    "算总价", "算总造价", "出总造价",
)
# 指向「总价/汇总」的计算信号（CALC_KW 子集，区别于纯换算）——配 cost 意图即判完整组价形态。
TOTAL_CALC_KW: tuple[str, ...] = ("总造价", "总价", "汇总", "合价")
# 规范/计量/计价口径信号（FR-K）：兜底归 norm，亦用于消歧。
NORM_KW: tuple[str, ...] = (
    "计算规则", "按什么计量", "怎么计量", "工程量怎么", "工程量如何", "计量单位", "规范",
    "条文", "条款", "项目特征", "工作内容", "适用范围", "章节说明", "包含哪些", "构成",
    "区别", "差异", "怎么描述", "如何描述",
)
# 泛词 norm 信号（意图混合路由）：这些词**恰在 NORM_KW 里但语义含糊**——「A 和 B 的区别/构成/
# 包含哪些」既可能是规范问答，也可能是被口语裹住的组价/价格诉求。落到 norm 且**只**命中这些泛词
# （或纯默认、且无版本锁）→ 判**低置信**，交 LLM 兜底复核（不把「未命中/只命中泛词」硬吞成 norm）。
GENERIC_NORM_KW: tuple[str, ...] = ("区别", "差异", "包含哪些", "构成")
# 合法能力枚举（LLM 兜底分类的校验白名单；越界 → fail-safe 跌回确定性默认）。
# out_of_domain（M1 补域外出口）：与造价领域完全无关（闲聊/天气/写代码…）——确定性层无法穷举
# 域外说法，故只由 LLM 兜底分类判出；编排器据此顶层直答能力范围，不进检索/取数管道白跑。
VALID_CAPABILITIES: tuple[str, ...] = ("norm", "cost", "price", "compound", "out_of_domain")

# ── 会话粘性（EH-05 扩展 · M1）：承接语词表——本句无任何强信号且含承接语时，沿用上一轮能力 ──
# 只治「就按你说的组一下」「继续」这类指代承接句；新话题（换问构成/区别等）不含承接语、不粘。
CONTINUATION_KW: tuple[str, ...] = (
    "就按", "按你说的", "按刚才", "刚才那", "刚才的", "刚才说的", "那就", "继续", "接着",
    "还是这个", "这个吧", "再来一个", "再算一个", "同上", "老样子", "跟上面一样", "跟刚才一样",
)
# 可粘能力：compound（一次性拆解）与 out_of_domain（域外不该延续）不粘。
STICKY_CAPABILITIES: tuple[str, ...] = ("norm", "cost", "price")

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

# ── 跨地域检测（EH-03，仅组价/价格侧）：本系统组价/价格严格锁深圳口径（C-02 分侧），用户显式
# 要他省/他市口径 → 不路由到取数（防按深圳数据答他省问题），出「体面告知」模板。规范问答（norm）
# 不在此拦——跨口径归 EH-05 确认后走联网兜底（FR-K07），是合法路径。
# 只列常问的省/市级地名（穷举不现实，漏判的兜底是取数层零命中→C-03 拒答，不会给错数据）。
OTHER_REGION_KW: tuple[str, ...] = (
    "北京", "上海", "广州", "天津", "重庆", "杭州", "南京", "武汉", "成都", "西安", "长沙",
    "东莞", "佛山", "珠海", "惠州", "中山", "厦门", "福州", "南宁", "海南", "河北", "山东",
    "河南", "湖北", "湖南", "四川", "江苏", "浙江", "安徽", "江西", "云南", "贵州", "陕西",
)


def detect_out_of_scope_region(text: str) -> str | None:
    """检测显式他省/他市口径诉求（EH-03）。参数：text 用户请求。返回：命中的地名或 None。

    「广东」不算出界（深圳属广东、常见于"广东省标"类表述归 norm 侧处理）；「深圳」出现时
    即便同句带他省名（如"深圳和北京对比"）也不拦——对比类会走复合/规范侧。
    """
    if "深圳" in text:
        return None
    hits = [kw for kw in OTHER_REGION_KW if kw in text]
    return hits[0] if hits else None


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
        out_of_scope_region —— 仅 cost/price（EH-03）：显式他省/他市口径的地名；命中则下游出
          「体面告知」模板、不做取数（组价/价格严格锁深圳，C-02 分侧）。
        route_confidence —— "high"/"low"：确定性判定链是否由**强信号**拍板（意图混合路由）。
          low = 落到 norm 兜底且只靠泛词/纯默认、且无版本锁 → 交 LLM 兜底复核。**红线闸（EH-03
          出界 / caliber 口径）无论置信高低都确定性判、LLM 不碰**。
        route_source —— "deterministic"/"llm_fallback"/"session_sticky"：capability 由谁拍板
          （确定性词表 / LLM 兜底分类 / 承接句沿用上一轮，审计升级率用）。
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
    out_of_scope_region: str | None = None
    compose_full: bool = False
    route_confidence: str = "high"
    route_source: str = "deterministic"
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
            "out_of_scope_region": self.out_of_scope_region,
            "compose_full": self.compose_full,
            "route_confidence": self.route_confidence,
            "route_source": self.route_source,
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


def route(query: str, *, has_project_context: bool | None = None,
          capability_override: str | None = None,
          prior_capability: str | None = None) -> RouteDecision:
    """确定性前置路由：query（+ 可选「是否已挂项目上下文」覆盖）→ RouteDecision。

    参数：
        query —— 用户自然语言请求。
        has_project_context —— 调用方已知是否挂了 BOQ/算量（覆盖文本推断）；None 则只看文本。
        capability_override —— **意图混合路由专用**：LLM 兜底分类判出的 capability
          （∈ VALID_CAPABILITIES）。给定即**跳过确定性能力分流**、以它为准，但**所有形态/红线闸
          （caliber / feature / EH-03 出界 / compose_full）照旧确定性重推**——LLM 只补能力分类、
          绝不碰安全闸。非法值忽略、退回确定性分流。
        prior_capability —— **会话粘性（EH-05 扩展）**：上一轮已定的能力落点。仅当本句
          无任何强信号（纯默认落 norm）**且含承接语**（CONTINUATION_KW）时沿用（route_source
          标 session_sticky）；形态/红线闸随新能力照常确定性重推，出界/特征/口径不粘。
    返回：RouteDecision。**本函数纯确定性、零 LLM**（override 由上层 route_hybrid 先算好再传入）。
    """
    text = query or ""
    reasons: list[str] = []

    cost_sig = _hits(text, COST_INTENT_KW)
    # 列清单意图：词表 + 柔性正则；裸「列项」二义（金标 A9「能同时列项吗」是 norm），
    # 只在伴随「清单/构件」时计入（如「清单列项」「把这几个构件列项」）。
    listing_sig = _hits(text, LISTING_INTENT_KW)
    if not listing_sig and _LISTING_RE.search(text):
        listing_sig = ["列…清单(柔性)"]
    if not listing_sig and "列项" in text and ("清单" in text or "构件" in text):
        listing_sig = ["列项(伴随清单/构件)"]
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
    # 复合判定只认价格**强信号**（信息价/市场价/趋势…或 材料+时间）——泛化"多少钱"不升复合。
    price_strong = bool(_hits(text, PRICE_STRONG_KW) or (material_sig and time_sig))
    explicit_compound = bool(compound_sig) or compare_hit
    cost_action = bool(cost_sig or listing_sig)  # 组价类动作词（套码/组价/列清单）
    multi_capability = (
        (cost_action and bool(norm_sig))         # 组价动作 + 规范口径（如「套定额 + 按什么计量」）
        or (price_strong and cost_action)        # 价格（强）+ 组价
        or (price_strong and bool(norm_sig))     # 价格（强）+ 规范
    )

    # ── 能力分流（优先级：复合 > 价格 > 组价 > 上下文核对 > 规范兜底）──
    #   意图混合路由：capability_override 给定（LLM 兜底判的能力）→ 直接采信、跳过确定性分流；
    #   源标 llm_fallback。非法值忽略、退回确定性。**强信号命中率（strong_signal）仍照确定性算**，
    #   仅用于置信度审计。
    strong_signal = bool(
        cost_sig or listing_sig or price_strong or explicit_compound or multi_capability
        or _EXPLICIT_GB_RE.search(text))
    route_source = "deterministic"
    if capability_override in VALID_CAPABILITIES:
        capability = capability_override
        source_type = "dynamic" if capability == "price" else "static"
        route_source = "llm_fallback"
        reasons.append(f"能力由 LLM 兜底分类判定：{capability}（确定性置信低 → 意图混合路由升级）")
    elif explicit_compound or multi_capability:
        capability, source_type = "compound", "static"
        if explicit_compound:
            reasons.append(
                f"复合/高阶信号 {compound_sig or '哪…更省(柔性匹配)'} → 先拆解（EH-01/FR-X）")
        else:
            reasons.append(
                f"多能力并列（组价={cost_sig} 价格={price_like} 规范={norm_sig}）→ 判复合先拆解（EH-01）")
    elif price_strong or (price_like and not cost_sig):
        # 泛化问价（"多少钱"）仅在无组价动作词时归 price——"组价多少钱"是组价诉求的口语尾巴，归 cost。
        capability, source_type = "price", "dynamic"
        reasons.append(f"价格取数信号 {price_sig or (material_sig + time_sig)} → 动态价格")
    elif cost_sig or listing_sig:
        capability, source_type = "cost", "static"
        if cost_sig:
            reasons.append(f"组价意图 {cost_sig} → 组价 Agent")
        else:
            reasons.append(f"列清单/列项意图 {listing_sig} → 组价能力（批量列清单落点在 M2，先归 cost 防误落 norm）")
    elif ctx_check_sig:
        capability, source_type = "cost", "static"
        reasons.append(f"项目核对 {ctx_check_sig} → 组价 + 上下文（FR-C）")
    else:
        capability, source_type = "norm", "static"
        reasons.append(f"无组价/价格意图{'，规范信号 ' + str(norm_sig) if norm_sig else '（默认）'} → 规范问答")

    # ── 会话粘性（EH-05 扩展 · M1）：承接句 + 上一轮能力 → 沿用 ──
    # 条件（全部满足才粘）：① 无 LLM override；② 本句无强信号（纯默认落 norm 的路径）；
    # ③ 含承接语；④ prior ∈ STICKY_CAPABILITIES。形态/红线闸随新能力在下文照常确定性重推
    # （他省出界/缺特征/口径不因粘性豁免）；粘性置信记 high（能力已定，不再走 LLM 兜底）。
    continuation_sig = _hits(text, CONTINUATION_KW)
    if (capability_override is None and prior_capability in STICKY_CAPABILITIES
            and capability == "norm" and not strong_signal and continuation_sig):
        capability = prior_capability
        source_type = "dynamic" if capability == "price" else "static"
        route_source = "session_sticky"
        reasons.append(f"承接语 {continuation_sig} + 上一轮能力 {prior_capability} → 会话粘性沿用（EH-05 扩展）")

    # ── 口径完整度（§4.0）：版本明确（2013/2024），或**显式点名了规范号**（如「按 gb50016」）——
    # 点名规范号=口径已明确表达，无需再反问版本；是否收录/可答由下游（检索零召回→FR-K07 兜底/拒答）裁定。
    _, version = family_version_of(text)
    caliber_complete = version is not None or bool(_EXPLICIT_GB_RE.search(text))

    # ── 形态：特征完整度（仅 cost，EH-04）+ 反问裁定 + 跨地域出界（EH-03）──
    feature_complete: bool | None = None
    clarify: str | None = None
    out_of_scope_region: str | None = None
    if capability == "cost":
        feature_complete = _has_component(text) and _has_feature_qualifier(text)
        if not feature_complete:
            clarify = "feature"  # EH-04 反问特征
            reasons.append("构件特征不足（缺强度/规格/材料）→ 反问特征（EH-04）")
        # §8 块1：cost 版本缺**不反问**，默认深圳2013；仅标 caliber 状态供口径声明。
        if not caliber_complete:
            reasons.append("版本未给 → 默认深圳·2013 口径，不反问（§4.0/T9-1）")
    elif capability == "norm":
        if not caliber_complete:
            clarify = "caliber"  # EH-05 会话粘性反问口径（软度在下游）
            reasons.append("规范问答缺口径（版本）→ 反问口径（EH-05，会话内仅首次）")

    # EH-03：组价/价格显式他省口径 → 出界标记（体面告知在下游编排器，不做取数）。
    if capability in ("cost", "price"):
        out_of_scope_region = detect_out_of_scope_region(text)
        if out_of_scope_region:
            clarify = None  # 出界告知优先于特征反问：先说清超范围，别先问特征
            reasons.append(f"显式他省口径「{out_of_scope_region}」→ 超出深圳范围，体面告知（EH-03）")

    # ── 完整组价形态（Option B）：cost + 「到总造价/逐步确认」信号 → 编排器点火 HITL 会话 ──
    #   （vs 一次性选码取数）。出界他省（out_of_scope）优先体面告知、不点火——由编排器据此二者裁定。
    compose_full = False
    if capability == "cost":
        full_sig = _hits(text, COMPOSE_FULL_KW) + _hits(text, TOTAL_CALC_KW)
        compose_full = bool(full_sig)
        if compose_full:
            reasons.append(f"完整组价形态 {full_sig} → 点火 HITL 会话（到总造价/逐步确认，Option B）")

    # ── 意图数量（EH-01）──
    intent_count = "compound" if capability == "compound" else "single"

    # ── 置信度（意图混合路由）：判定链是否由强信号拍板 ──
    #   low ⟺ 落到 norm 兜底 且 只命中泛词/纯默认（无非泛词 norm 实词）且 无版本锁（无 2013/2024、无显式GB）。
    #   这类「未命中/只命中泛词」恰是关键词穷举疲劳 + 口语变体漏判的重灾区 → 交 LLM 兜底复核。
    #   cost/price/compound 均由强信号驱动（strong_signal），一律 high、零延迟直配、金标可回归。
    #   LLM 兜底改判后（route_source=llm_fallback）不再回炉，置信随新能力重算（多为 high）。
    strong_norm = bool(set(norm_sig) - set(GENERIC_NORM_KW))
    route_confidence = (
        "low" if (capability == "norm" and not strong_norm and not caliber_complete)
        else "high")
    if route_source == "session_sticky":
        route_confidence = "high"  # 粘性已定能力（含粘回 norm），无需再走 LLM 兜底

    return RouteDecision(
        capability=capability, source_type=source_type, needs_calc=needs_calc,
        needs_context=needs_context, intent_count=intent_count,
        feature_complete=feature_complete, caliber_complete=caliber_complete,
        clarify=clarify, out_of_scope_region=out_of_scope_region,
        compose_full=compose_full,
        route_confidence=route_confidence, route_source=route_source,
        matched={
            "cost": cost_sig, "listing": listing_sig, "price": price_sig, "compound": compound_sig,
            "continuation": continuation_sig, "compare": compare_hit, "multi_capability": multi_capability,
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
    # ── EH-03 跨地域出界（仅 cost/price；出界告知优先于特征反问）──
    ("这个柱子按北京定额套价", "cost", None),                    # 他省组价→出界，不反问特征
    ("上海本月钢筋信息价多少", "price", None),                   # 他省价格→出界
    ("深圳本月钢筋信息价多少", "price", None),                   # 深圳→不出界（对照）
    # ── 列清单/列项（M1 补盲区）：归 cost，缺构件特征→feature 反问（不再误落 norm 反问版本）──
    ("帮我列一下清单", "cost", "feature"),                       # 柔性正则「列一下清单」
    ("给这个项目列清单", "cost", "feature"),
    ("根据设计说明编制工程量清单", "cost", "feature"),
    ("清单列项", "cost", "feature"),
    ("帮我把这几个构件列项", "cost", "feature"),                 # 「列项」伴随「构件」才计
    ("这个工程开个清单", "cost", "feature"),                     # 柔性正则「开个清单」
    # 反例·裸「列项」无清单/构件伴随 → 仍是 norm（金标 A9 同款句式，不误吞）
    ("综合脚手架和单项脚手架能同时列项吗", "norm", "caliber"),
)


# EH-03 出界地名期望（None=不出界），与 _SELFTEST_CASES 尾部三例对应。
_SELFTEST_REGION_CASES: tuple[tuple[str, str | None], ...] = (
    ("这个柱子按北京定额套价", "北京"),
    ("上海本月钢筋信息价多少", "上海"),
    ("深圳本月钢筋信息价多少", None),
    ("矩形柱按什么规则计量？", None),                            # norm 不拦跨地域
)


# 置信度期望（意图混合路由）：high=强信号确定性直配，low=只泛词/纯默认 norm 兜底→交 LLM 复核。
_SELFTEST_CONFIDENCE_CASES: tuple[tuple[str, str], ...] = (
    # ── high：强信号（cost/price/compound/显式GB/强 norm 实词/版本锁）──
    ("C30现浇混凝土矩形柱怎么组价？", "high"),                    # 组价动作词
    ("深圳本月HRB400钢筋信息价是多少？", "high"),                 # 价格强信号
    ("这两种方案哪种更省", "high"),                              # 复合
    ("按 gb50500-2024 综合单价包含哪些费用？", "high"),           # 显式GB（即便含泛词「包含哪些」）
    ("满堂脚手架工程量怎么计算？", "high"),                       # 强 norm 实词「工程量怎么」
    ("房建计量规范 2013 和 2024 墙面抹灰有什么区别？", "high"),    # 版本锁（+「规范」实词）
    # ── low：未命中/只命中泛词、无版本锁 → 落 norm 兜底、交 LLM 复核 ──
    ("综合单价的构成是什么", "low"),                             # 只命中泛词「构成」
    ("这两个有什么区别", "low"),                                 # 只命中泛词「区别」
    ("帮我把这根柱子弄一下", "low"),                             # 口语变体、无任何关键词（纯默认）
    ("独立基础和条形基础包含哪些", "low"),                       # 只命中泛词「包含哪些」，无版本
    ("帮我列一下清单", "high"),                                  # 列清单强信号→直配 cost，不调兜底
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

    # EH-03 出界地名
    for query, exp_region in _SELFTEST_REGION_CASES:
        d = route(query)
        ok = d.out_of_scope_region == exp_region
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"{'✓' if ok else '✗'} region={str(d.out_of_scope_region):<5} (期望 {exp_region})  {query[:30]}")

    # route_confidence（意图混合路由：高=强信号直配 / 低=交 LLM 兜底复核）
    for query, exp_conf in _SELFTEST_CONFIDENCE_CASES:
        d = route(query)
        ok = d.route_confidence == exp_conf
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"{'✓' if ok else '✗'} conf={d.route_confidence:<4} (期望 {exp_conf}) cap={d.capability:<8} {query[:26]}")

    # capability_override（LLM 兜底注入）：能力改判但红线闸仍确定性重推
    d_ovr = route("帮我把这根柱子弄一下", capability_override="cost")
    ovr_ok = (d_ovr.capability == "cost" and d_ovr.route_source == "llm_fallback"
              and d_ovr.clarify == "feature")  # 缺特征→确定性重推出 feature 反问
    passed += ovr_ok
    failed += not ovr_ok
    print(f"{'✓' if ovr_ok else '✗'} override→cost src={d_ovr.route_source} clarify={d_ovr.clarify}（红线闸确定性重推）")

    # capability_override→out_of_domain（M1 域外出口）：域外标签生效、不触发任何反问/出界闸
    d_ood = route("今天天气怎么样", capability_override="out_of_domain")
    ood_ok = (d_ood.capability == "out_of_domain" and d_ood.route_source == "llm_fallback"
              and d_ood.clarify is None and d_ood.out_of_scope_region is None)
    passed += ood_ok
    failed += not ood_ok
    print(f"{'✓' if ood_ok else '✗'} override→out_of_domain src={d_ood.route_source} clarify={d_ood.clarify}（域外直答出口）")

    # 会话粘性（EH-05 扩展）：承接句沿用上一轮能力；无承接语/无 prior/域外 prior 不粘
    sticky_checks = (
        (route("就按你说的组一下吧", prior_capability="cost"), "cost", "session_sticky"),
        (route("继续", prior_capability="price"), "price", "session_sticky"),
        (route("综合单价的构成是什么", prior_capability="cost"), "norm", "deterministic"),   # 新话题无承接语→不粘
        (route("就按你说的组一下吧"), "norm", "deterministic"),                              # 无 prior→不粘
        (route("就按你说的来", prior_capability="out_of_domain"), "norm", "deterministic"),  # 域外 prior→不粘
    )
    for d_s, exp_cap, exp_src in sticky_checks:
        ok = d_s.capability == exp_cap and d_s.route_source == exp_src
        passed += ok
        failed += not ok
        print(f"{'✓' if ok else '✗'} sticky cap={d_s.capability:<8} src={d_s.route_source:<15} "
              f"(期望 {exp_cap}/{exp_src}) conf={d_s.route_confidence}")

    total = (len(_SELFTEST_CASES) + len(_SELFTEST_REGION_CASES)
             + len(_SELFTEST_CONFIDENCE_CASES) + 2 + len(sticky_checks))
    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
