"""cost_task（L6-A 端到端组价）终态 + 红线 + pass^k 打分器 —— 纯函数、无 I/O、无 LLM。

判定口径（见 L6_agent/README「三条铁律」+ AGENT_BENCHMARK §2-L6）：
- **比最终状态，不比工具名/答案文本**：终态核心是 agent 落定的 9 位清单码是否 == 金标。
- **红线 policy 独立计分**：不糅进任务成功率；任一红线违规 → 该次运行 redline fail。
- **pass^k 而非 pass@k**：一条 case 连跑 pass_k 次全过才算过（测 8B 一致性、不漂移）。

**诚实原则**：只判「外部可观测」的项。无法从 agent 运行观测可靠判定的（如 `no_rag_calc`
是否真在 RAG 层算了数、`no_fabricate_code` 码是否在候选内）标 ``None`` = not_evaluable，
**绝不假装通过**；聚合时单列 evaluable 覆盖率，避免"未判的当过了"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 9 位全国清单码；12 位含顺序码时取前 9 位比对（终态比编码语义、不比顺序号）。
_CODE_RE = re.compile(r"\b(\d{9})(?:\d{3})?\b")
_REFUSE_KW = ("拒答", "超出", "超范围", "不覆盖", "仅覆盖", "仅支持", "无法组价", "不组价", "建议当地", "本系统仅")
_CALIBER_KW = ("口径", "深圳·2013", "深圳·2024", "深圳 2013", "深圳 2024", "默认深圳")
_FLAG_KW = ("需人工", "存疑", "待核", "请复核", "需复核", "不确定", "提示", "标注", "无法唯一", "多个候选")
_OTHER_PROVINCE = ("北京", "上海", "广州", "广东省", "浙江", "江苏", "四川")


@dataclass
class RunObservation:
    """一次 agent 运行的外部观测（由 runner 从 DeerFlowClient 流里提取，判定只依赖这些）。

    字段：codes 运行中出现过的清单码（按出现序，来自工具结果 + 最终答案）；final_code agent 最终
      落定/选中的码（None=没落码）；tools 调用过的工具名；answer 最终答案文本；did_clarify 是否
      触发过 ask_clarification（含 harness 哑火收编的 ToolMessage）。
    """

    codes: list[str] = field(default_factory=list)
    final_code: str | None = None
    tools: list[str] = field(default_factory=list)
    answer: str = ""
    did_clarify: bool = False


@dataclass
class RunScore:
    """一次运行的判定结果。terminal/policy 值为 True/False/None(not_evaluable)。"""

    terminal: dict[str, bool | None]
    policy: dict[str, bool | None]

    @property
    def task_pass(self) -> bool:
        """所有**可判**终态项均过（无可判项时视为未过，防空过）。"""
        vals = [v for v in self.terminal.values() if v is not None]
        return bool(vals) and all(vals)

    @property
    def redline_ok(self) -> bool:
        """无任何红线违规（None 不算违规，只有显式 False 才算）。"""
        return not any(v is False for v in self.policy.values())


def _norm_code(s: str | None) -> str | None:
    """从字符串取规范化 9 位清单码（12 位取前 9）；无则 None。"""
    if not s:
        return None
    m = _CODE_RE.search(str(s))
    return m.group(1) if m else None


def _norm_std(s: str) -> str:
    """标准号归一：去空格、去横杠、大写（``GB 50854`` / ``GB50854-2024`` → ``GB50854...``）。"""
    return re.sub(r"[\s\-]", "", str(s)).upper()


def check_terminal(case: dict[str, Any], obs: RunObservation) -> dict[str, bool | None]:
    """判终态（外部可观测项）；返回 {check_name: True/False/None}。None=不可判、不计分。"""
    tc = case.get("terminal_check") or {}
    out: dict[str, bool | None] = {}
    ans = obs.answer or ""

    # 核心：落定的 9 位清单码 == 金标（final_code 优先；无 final 用出现过的码集合兜底）。
    exp_code = _norm_code(tc.get("expected_bill_code"))
    if exp_code is not None:
        final = _norm_code(obs.final_code)
        if final is not None:
            out["expected_bill_code"] = (final == exp_code)
        else:
            seen = {_norm_code(c) for c in obs.codes}
            out["expected_bill_code"] = (exp_code in seen) if seen else False

    # 溯源：每条 must_cite 的标准号(归一) + 版本都出现在答案里。
    must_cite = tc.get("must_cite")
    if must_cite:
        norm_ans = _norm_std(ans)
        ok = True
        for c in must_cite:
            std_ok = _norm_std(c.get("std", "")) in norm_ans if c.get("std") else True
            ver_ok = str(c.get("version", "")) in ans if c.get("version") else True
            ok = ok and std_ok and ver_ok
        out["must_cite"] = ok

    # 缺特征须反问。
    if tc.get("must_ask"):
        out["must_ask"] = obs.did_clarify

    # 跨省须拒答：命中拒答语 且 没有把他省的期望码"取出来当答案"（拒答不落码）。
    if tc.get("must_refuse"):
        refused = any(k in ans for k in _REFUSE_KW)
        out["must_refuse"] = refused and obs.final_code is None

    # 须声明口径。
    if tc.get("must_declare_caliber"):
        out["must_declare_caliber"] = any(k in ans for k in _CALIBER_KW)

    # 须标注/提示（如 no_fabricate 场景的"无法唯一、需人工"）。
    if tc.get("must_flag"):
        out["must_flag"] = any(k in ans for k in _FLAG_KW)

    # 复合须输出（如两版对比）：diff_source 里的关键词都出现（best-effort）。
    if tc.get("must_output"):
        toks = tc.get("diff_source") or tc.get("must_output")
        toks = toks if isinstance(toks, list) else [toks]
        out["must_output"] = all(str(t) in ans for t in toks) if toks else None

    return out


def check_policy(case: dict[str, Any], obs: RunObservation) -> dict[str, bool | None]:
    """判红线 policy（独立计分）；可观测→True/False，不可靠观测→None(诚实标注)。"""
    pol = case.get("policy") or {}
    out: dict[str, bool | None] = {}
    ans = obs.answer or ""

    # 缺特征就必须先反问（missing_feature 档最相关；其余档无缺特征→真空满足）。
    if "clarify_if_missing_feature" in pol:
        if case.get("difficulty") == "missing_feature" or (case.get("terminal_check") or {}).get("must_ask"):
            out["clarify_if_missing_feature"] = obs.did_clarify
        else:
            out["clarify_if_missing_feature"] = True  # 无缺特征，真空满足

    # 地域不串库：答案里没出现他省口径泄漏（"无观测到泄漏"语义，非"证明干净"）。
    if "no_region_leak" in pol:
        leaked = any((p in ans) for p in _OTHER_PROVINCE)
        out["no_region_leak"] = (not leaked)

    # 以下红线**外部不可靠观测**，诚实标 None（不假装通过）：
    #   no_rag_calc          —— 是否在 RAG 层算了数，外部看不到
    #   no_fabricate_code    —— 码是否在候选内（外部无候选集）；仅能验格式，不足以判"不编造"
    #   no_silent_version_switch / diff_deterministic —— 需内部状态
    for k in ("no_rag_calc", "no_fabricate_code", "no_silent_version_switch", "diff_deterministic"):
        if k in pol:
            out[k] = None

    return out


def score_run(case: dict[str, Any], obs: RunObservation) -> RunScore:
    """判一次运行 = 终态 + 红线。"""
    return RunScore(terminal=check_terminal(case, obs), policy=check_policy(case, obs))


@dataclass
class CaseResult:
    """一条 case 的 pass^k 聚合。"""

    case_id: str
    difficulty: str
    pass_k: int
    n_runs: int
    task_passk: bool          # 连跑 pass_k 次终态全过（不含红线）
    redline_clean: bool       # pass_k 次里无任何红线违规
    overall_pass: bool        # 终态全过 且 无红线违规
    runs: list[RunScore]


def aggregate_passk(case: dict[str, Any], runs: list[RunScore]) -> CaseResult:
    """把一条 case 的 pass_k 次运行聚合成 pass^k 结果（连跑全过才算过）。"""
    pass_k = int(case.get("pass_k") or 1)
    n = len(runs)
    enough = n >= pass_k
    task_passk = enough and all(r.task_pass for r in runs)
    redline_clean = all(r.redline_ok for r in runs)
    return CaseResult(
        case_id=case.get("id", "?"),
        difficulty=case.get("difficulty", "?"),
        pass_k=pass_k,
        n_runs=n,
        task_passk=task_passk,
        redline_clean=redline_clean,
        overall_pass=task_passk and redline_clean,
        runs=runs,
    )


def aggregate_suite(results: list[CaseResult]) -> dict[str, Any]:
    """整套聚合：任务成功率(pass^k) + 红线违规率(独立) + 逐 difficulty + evaluable 覆盖率。"""
    n = len(results)

    def _rate(pred) -> float:
        return (sum(1 for r in results if pred(r)) / n) if n else float("nan")

    # evaluable 覆盖率：所有运行里，被实际判定(非 None)的红线检查占比——诚实暴露"多少红线其实没判"。
    pol_total = pol_eval = 0
    for r in results:
        for run in r.runs:
            for v in run.policy.values():
                pol_total += 1
                if v is not None:
                    pol_eval += 1

    by_diff: dict[str, dict[str, int]] = {}
    for r in results:
        d = by_diff.setdefault(r.difficulty, {"n": 0, "pass": 0})
        d["n"] += 1
        d["pass"] += int(r.overall_pass)

    return {
        "n_cases": n,
        "task_success_passk": _rate(lambda r: r.task_passk),        # 终态连跑全过
        "redline_violation_rate": _rate(lambda r: not r.redline_clean),  # 独立：越低越好，门=0
        "overall_pass_rate": _rate(lambda r: r.overall_pass),        # 终态过 且 无红线违规
        "policy_evaluable_coverage": (pol_eval / pol_total) if pol_total else float("nan"),
        "by_difficulty": {k: {**v, "pass_rate": (v["pass"] / v["n"]) if v["n"] else float("nan")}
                          for k, v in sorted(by_diff.items())},
    }
