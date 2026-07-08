"""规范选择确定化（T-A2）—— 「问题类型 → 规范代号」的确定性映射，把选哪部规范从弱模型手里夺回。

> 对应 PRD §4.0 口径归一只管
> 地域+版本，未管「选哪部 GB」——标准漂移 bug（50854→50500）即此缺口。

**为什么放服务端、不交给 LLM**：同 §8.3「红线放服务端、弱模型不碰」原则。让 Qwen3-8B 自由判断
选哪部规范，正是把「计量 vs 计价」这条对答案对错有决定性影响的轴交给最不可靠的环节——实测
弱模型会把「矩形柱按什么计量」（应 GB50854 计量）误路由到 GB50500 计价（标准漂移）。本模块用
**关键词规则**确定性地定 family，纯函数、零 LLM、可单测。

**三族（family）**：
  - ``gb50854`` 房屋建筑与装饰工程工程量**计算**规范 —— 房建/装饰构件的「计量规则」
  - ``gb50856`` 通用安装工程工程量**计算**标准 —— 给排水/暖通/电气/消防等「安装专业计量」
  - ``gb50500`` 建设工程工程量清单**计价**规范 —— 综合单价/取费/计税/清单计价口径（与专业无关）

**两轴判定**：
  1. intent（计价 vs 计量）：出现「综合单价/取费/费率/计税…」等钱的信号 → 计价(50500)；否则计量。
  2. discipline（房建 vs 安装，仅 intent=计量 时用）：出现「通风/电气/给排水/消防…」安装信号 → 50856；
     否则默认房建 50854。

**降级安全**：关键词零命中时**不硬猜**——回退到调用方给的 hint（若有且合法），无 hint 才默认
``gb50854``（房建计量最常见）并标 ``source=default``、``confidence=low``，交下游/人核。

**版本轴**（2013/2024）属 §8 块1/T9-1 的活，本模块只做轻量解析：query 内显式版本 > hint 版本 >
``DEFAULT_VERSION``。本模块**只对 family 负责**（T-A2 范围），版本默认策略不在此拍板。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# ── 合法规范代号（与 ce-code config.STANDARD_ALIASES 的造价计量计价规范一致）──
# 注意：gb50856 仅 2024 版（无 2013），见下 _clamp_version。
VALID_STANDARDS: frozenset[str] = frozenset({
    "gb50500-2013", "gb50500-2024",   # 工程量清单计价规范/标准（计价）
    "gb50854-2013", "gb50854-2024",   # 房屋建筑与装饰工程工程量计算规范/标准（房建计量）
    "gb50856-2024",                   # 通用安装工程工程量计算标准（安装计量）
})

# 版本默认：query / hint 都未带版本时兜底。属 §8 块1/T9-1 的策略点，这里仅给一个 env 可调缺省，
# 默认 2024（与现 benchmark 金标、且唯一三族齐全的版本一致）；T9-1 落地组价默认 2013 时由那条统管。
DEFAULT_VERSION: str = os.environ.get("CE_NORM_DEFAULT_VERSION", "2024")

# ── 关键词词表（确定性规则的全部依据）──
# 计价信号（→ gb50500）：均为「钱/费用/计价口径」无歧义词，避免误吞计量问题。
PRICING_KW: tuple[str, ...] = (
    "综合单价", "单价构成", "单价组成", "费用构成", "费用组成",
    "取费", "费率", "管理费", "利润", "规费", "税金", "计税", "增值税",
    "一般计税", "简易计税", "暂列金额", "暂估价", "计日工", "总承包服务费",
    "总包服务费", "风险费", "风险费用", "清单计价", "招标控制价", "投标报价",
    "工程造价", "造价构成", "措施费", "其他项目费", "分部分项工程费", "计价规范",
    "计价标准", "工程量清单计价",
)

# 安装专业信号（→ gb50856，仅计量时用）：机电/安装各专业。
INSTALL_KW: tuple[str, ...] = (
    "安装工程", "安装", "通风", "空调", "暖通", "采暖", "供暖", "燃气",
    "给排水", "给水", "排水", "雨水", "中水", "卫生器具",
    "电气", "配电", "配电箱", "配电柜", "电缆", "电线", "桥架", "母线", "防雷", "接地",
    "消防", "喷淋", "喷头", "消火栓", "防火阀", "排烟阀", "报警", "智能化", "弱电",
    "通信", "刷油", "防腐", "绝热", "保温(管道)",
    "风管", "风口", "管道", "阀门", "法兰", "支架", "设备安装", "机电",
    "水泵", "风机", "锅炉", "冷水机组", "工业管道",
)

# 房建/装饰构件 + 措施信号（→ gb50854，计量时的默认族）。
BUILDING_KW: tuple[str, ...] = (
    "土方", "石方", "土石方", "挖方", "填方", "回填", "地基", "桩", "桩基",
    "砌筑", "砖墙", "砌块", "砌块墙", "实心砖", "多孔砖", "空心砖", "毛石",
    "混凝土", "现浇", "预制", "柱", "梁", "板", "墙", "基础", "楼梯", "构造柱",
    "圈梁", "过梁", "雨篷", "阳台", "钢筋", "钢筋混凝土", "模板", "脚手架",
    "金属结构", "钢结构", "木结构", "木门", "门窗", "屋面", "防水", "卷材",
    "保温", "隔热", "楼地面", "地面", "踢脚", "墙面", "柱面", "抹灰", "饰面",
    "天棚", "吊顶", "涂料", "油漆", "裱糊", "幕墙", "栏杆", "扶手", "台阶", "散水",
    "垂直运输", "建筑面积", "装饰", "装修",
)

# 计量/计算规则的通用信号（推向「计量」轴，具体房建/安装由上面两表定）。
MEASURE_KW: tuple[str, ...] = (
    "计量", "计算规则", "工程量", "如何计算", "怎么计算", "怎么算", "按什么计量",
    "计量单位", "以体积", "以面积", "以质量", "以长度", "以重量",
    "按设计图示", "扣除", "不扣除", "并入", "净量", "工作内容", "项目特征",
)


@dataclass
class StandardResolution:
    """规范选择结果（确定性）。

    字段：
        standard —— 最终规范代号（如 ``gb50854-2024``），保证 ∈ VALID_STANDARDS。
        family —— 规范族（``gb50854`` / ``gb50500`` / ``gb50856``），不含版本。
        version —— ``2013`` / ``2024``。
        intent —— ``measurement``（计量） / ``pricing``（计价）。
        discipline —— ``building``（房建） / ``installation``（安装） / ``None``（计价无关专业）。
        confidence —— ``high``（关键词确定） / ``low``（回退 hint 或 default）。
        source —— ``deterministic`` / ``hint``（关键词零命中、用调用方 hint） / ``default``（都没有）。
        overrode_hint —— 确定性 family 与调用方 hint 的 family 冲突、已夺回时 True（标准漂移拦截点）。
        matched —— 命中的关键词（可观测/审计用）。
        requested —— 调用方传入的原始 standard hint（可空）。
        notes —— 额外说明（如 50856 仅 2024 版的版本钳制）。
    """

    standard: str
    family: str
    version: str
    intent: str
    discipline: str | None
    confidence: str
    source: str
    overrode_hint: bool
    matched: list[str] = field(default_factory=list)
    requested: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_meta(self) -> dict:
        """转成 meta 可观测字典（进 /norm/qa 响应 meta.standard_resolution）。"""
        return {
            "requested": self.requested,
            "resolved": self.standard,
            "family": self.family,
            "version": self.version,
            "intent": self.intent,
            "discipline": self.discipline,
            "confidence": self.confidence,
            "source": self.source,
            "overrode_hint": self.overrode_hint,
            "matched": self.matched,
            "notes": self.notes,
        }


_VERSION_RE = re.compile(r"(20\s*13|20\s*24)")
# 五位规范码（50500/50854/50856）可出现在任意串里：代号 gb50854-2024、store 名 GB_T50854-2024_…、
# 散写 GB/T 50854 均能认。版本（2013/2024）单独抠。
_CODE_RE = re.compile(r"(50500|50854|50856)")


def family_version_of(text: str | None) -> tuple[str | None, str | None]:
    """从任意规范串里**宽松**抽 (family, version)。

    认得：``gb50854-2024`` / ``GB_T50854-2024_房屋…``（store 名）/ ``GB/T 50854`` / ``50500`` 等；
    抽不到对应位返回 None。供 hint 解析与 guard 口径纯净校验复用（cited_clause.standard 即 store 名）。
    """
    if not text:
        return None, None
    code_m = _CODE_RE.search(text)
    family = ("gb" + code_m.group(1)) if code_m else None
    ver_m = _VERSION_RE.search(text)
    version = ver_m.group(1).replace(" ", "") if ver_m else None
    return family, version


def _count_hits(text: str, vocab: tuple[str, ...]) -> list[str]:
    """返回 text 中命中的关键词列表（去重、保序）。"""
    seen: list[str] = []
    for kw in vocab:
        if kw in text and kw not in seen:
            seen.append(kw)
    return seen


def _parse_version(text: str) -> str | None:
    """从文本里抠出显式版本（2013/2024），无则 None。"""
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return m.group(1).replace(" ", "")


def parse_hint(hint: str | None) -> tuple[str | None, str | None]:
    """解析调用方 hint（完整代号 ``gb50854-2024`` 或脏写）→ (family, version)。

    薄封装 ``family_version_of``；认不出返回 (None, None)。
    """
    return family_version_of(hint)


def _clamp_version(family: str, version: str) -> tuple[str, list[str]]:
    """把 (family, version) 钳制到实际存在的索引内；返回 (有效版本, notes)。

    gb50856 仅 2024 版：请求 2013 时降级到 2024 并出 note（不串库、不假装有 2013 安装索引）。
    """
    notes: list[str] = []
    candidate = f"{family}-{version}"
    if candidate in VALID_STANDARDS:
        return version, notes
    # 该 family 无此版本：取该 family 实际存在的版本兜底（目前只 50856-2013 会落到这）。
    available = sorted(v.split("-")[1] for v in VALID_STANDARDS if v.startswith(family + "-"))
    if not available:
        return version, notes  # 理论不可达：family 不在表内（resolve 前已保证）
    fallback = "2024" if "2024" in available else available[0]
    notes.append(f"{family} 无 {version} 版索引，降级到 {fallback} 版")
    return fallback, notes


def _classify_family(text: str) -> tuple[str | None, str, str | None, list[str]]:
    """关键词分类 → (family, intent, discipline, matched)。

    family 为 None 表示零命中（交 resolve 走 hint/default 降级）。
    """
    pricing = _count_hits(text, PRICING_KW)
    install = _count_hits(text, INSTALL_KW)
    building = _count_hits(text, BUILDING_KW)
    measure = _count_hits(text, MEASURE_KW)

    measure_total = len(install) + len(building) + len(measure)

    # ① 计价优先判定：有明确钱信号、且不被计量信号压制（计量信号更多则视为「带钱字眼的计量问题」）。
    if pricing and len(pricing) >= measure_total:
        return "gb50500", "pricing", None, pricing

    # ② 计量轴：安装专业信号占优 → 50856，否则房建 50854。
    if install and len(install) >= len(building):
        return "gb50856", "measurement", "installation", install + measure
    if building or measure:
        return "gb50854", "measurement", "building", building + measure

    # ③ 零命中：仅剩孤立的钱信号（measure_total==0 但上面 pricing>=0 已处理），或全空。
    if pricing:
        return "gb50500", "pricing", None, pricing
    return None, "measurement", "building", []


def resolve_standard(query: str, *, hint: str | None = None,
                     default_version: str = DEFAULT_VERSION) -> StandardResolution:
    """确定性地把「问题 + 可选 hint」解析为规范代号。

    参数：
        query —— 用户自然语言问题。
        hint —— 调用方（含 LLM）给的 standard，可为完整代号或脏写；仅作**备选**，确定性结果优先。
        default_version —— query/hint 均无版本时的兜底版本。
    返回：StandardResolution（standard 保证 ∈ VALID_STANDARDS）。

    family 优先级：确定性关键词 > hint > 默认 gb50854。
    version 优先级：query 显式 > hint 版本 > default_version。
    """
    text = query or ""
    hint_family, hint_version = parse_hint(hint)

    det_family, intent, discipline, matched = _classify_family(text)

    # ── 定 family（含「夺回」逻辑）──
    overrode = False
    if det_family is not None:
        family = det_family
        confidence = "high"
        source = "deterministic"
        if hint_family and hint_family != det_family:
            overrode = True  # 标准漂移拦截：LLM 选错族，确定性夺回
    elif hint_family:
        family = hint_family
        confidence = "low"
        source = "hint"
        # 零命中时无法判 intent/discipline，按 hint family 回填一个合理标注
        intent = "pricing" if family == "gb50500" else "measurement"
        discipline = None if family == "gb50500" else (
            "installation" if family == "gb50856" else "building")
    else:
        family = "gb50854"
        confidence = "low"
        source = "default"
        intent, discipline = "measurement", "building"

    # ── 定 version（query 显式 > hint > default）──
    version = _parse_version(text) or hint_version or default_version

    version, notes = _clamp_version(family, version)
    standard = f"{family}-{version}"

    return StandardResolution(
        standard=standard, family=family, version=version, intent=intent,
        discipline=discipline, confidence=confidence, source=source,
        overrode_hint=overrode, matched=matched, requested=hint, notes=notes,
    )


# ─────────────────────────── 内置自测（无需服务、无需 LLM）───────────────────────────
# 运行：cd ce-services && uv run python -m norm.standard_router
# 断言每条问题解析到正确 family（含 50854→50500 漂移反例）。
_SELFTEST_CASES: tuple[tuple[str, str | None, str], ...] = (
    # (query, hint, 期望 family)
    # ── 计量（房建 50854）──
    ("矩形柱按什么规则计量？", None, "gb50854"),
    ("满堂脚手架工程量怎么计算？", None, "gb50854"),
    ("现浇混凝土柱的项目特征应该怎么描述？", None, "gb50854"),
    ("实心砖墙的工程量计算规则是什么", None, "gb50854"),
    ("钢筋按什么计量单位计算", None, "gb50854"),
    ("墙面抹灰的工程量如何计算，扣除门窗洞口吗", None, "gb50854"),
    # ── 计量（安装 50856）──
    ("通风管道的防火阀怎么计量？", None, "gb50856"),
    ("给排水管道工程量怎么算", None, "gb50856"),
    ("电气配电箱的安装工程量计算规则", None, "gb50856"),
    ("消防喷淋喷头按什么计量", None, "gb50856"),
    # ── 计价（50500）──
    ("综合单价由哪些费用构成？", None, "gb50500"),
    ("一般计税和简易计税在取费上有什么区别", None, "gb50500"),
    ("措施项目费的费率怎么取", None, "gb50500"),
    ("暂列金额和暂估价怎么计价", None, "gb50500"),
    # ── 标准漂移反例：计量问题 + LLM 误给 50500 hint → 应夺回 50854/50856 ──
    ("矩形柱按什么计量", "gb50500-2024", "gb50854"),
    ("通风管道防火阀怎么计量", "gb50500-2024", "gb50856"),
    # ── hint 与确定性一致：不算 override ──
    ("综合单价包含哪些费用", "gb50500-2024", "gb50500"),
    # ── 零命中 → 回退 hint ──
    ("这个怎么处理", "gb50856-2024", "gb50856"),
    # ── 版本解析 ──
    ("按 2013 版房建计量规范，实心砖墙怎么计量", None, "gb50854"),
)


def _selftest() -> int:
    passed = failed = 0
    overrides = 0
    for query, hint, expect_family in _SELFTEST_CASES:
        r = resolve_standard(query, hint=hint)
        ok = r.family == expect_family and r.standard in VALID_STANDARDS
        if r.overrode_hint:
            overrides += 1
        flag = "✓" if ok else "✗"
        if ok:
            passed += 1
        else:
            failed += 1
        extra = " [夺回]" if r.overrode_hint else ""
        print(f"{flag} {r.standard:<14} <- {query[:28]:<28} hint={hint or '-':<14}"
              f" intent={r.intent} src={r.source}{extra}")
        if not ok:
            print(f"    期望 family={expect_family}，实得 {r.family}；matched={r.matched}")
    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {len(_SELFTEST_CASES)}；夺回(override) {overrides} 例")
    # 版本钳制单测：50856 + 2013 → 降级 2024
    r56 = resolve_standard("给排水管道工程量怎么算", hint="gb50856-2013")
    assert r56.standard == "gb50856-2024" and r56.notes, "50856 版本钳制失败"
    print(f"✓ 版本钳制：gb50856-2013 → {r56.standard}（{r56.notes[0]}）")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    # Windows 控制台默认 gbk，✓/✗ 等字符会 UnicodeEncodeError；统一切 utf-8（Linux 无副作用）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(_selftest())
