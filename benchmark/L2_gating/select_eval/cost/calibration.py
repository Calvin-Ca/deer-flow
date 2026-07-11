"""选码置信外部校准（路 2）—— 用检索几何客观校准 LLM 自报置信。

背景（TODO 主线二「置信度无区分度·治本」）：Qwen3-8B 自报 confidence 几乎恒为 0.95，致
`confidence<阈值` 兜底与 HITL 编码闸从不触发、「高置信错码须为 0」红线结构性失效。本模块**不信自报**，
改用 `bill_match` 召回的 cosine score 算一个**客观置信**，再与自报**保守取 min**——外部信号只会把虚高
置信拉低、触发复核，**绝不抬高**（min 语义：任一信号说「没把握」就没把握）。

两个客观信号（都来自候选 score，天然有区分度）：
- **绝对贴合度** `_abs_conf`：选中候选 cosine 越高越可信（< FLOOR→0 不贴切 / > CEIL→1 贴切 / 之间线性内插）。
- **间距** `_margin_conf`：选中候选比其余候选高出越多越无歧义；若选中候选反而**低于**某个其它候选
  （LLM 逆检索而选）→ 间距为负 → 归 0（强复核信号，正是「高置信错码」高发处）。
外部置信 = 两信号取 min（保守）。最终 effective = min(自报, 外部)。参数见 `common.config`（env 可调）。

纯函数、无 IO/LLM，可本地直接单测。
"""
from __future__ import annotations


def _clamp01(x: float) -> float:
    """夹到 [0,1]。"""
    return min(1.0, max(0.0, x))


def _abs_conf(chosen_score: float, floor: float, ceil: float) -> float:
    """绝对贴合度信号：cosine [floor, ceil] 线性映射到 [0,1]。

    参数：chosen_score —— 选中候选 cosine；floor/ceil —— 不贴切/贴切阈值。
    返回：[0,1]。ceil<=floor（非法配置）退化为阶跃（>=ceil→1 否则 0），不抛。
    """
    if ceil <= floor:
        return 1.0 if chosen_score >= ceil else 0.0
    return _clamp01((chosen_score - floor) / (ceil - floor))


def _margin_conf(chosen_score: float, runner_up_score: float | None, margin_full: float) -> float:
    """间距信号：选中候选与次优候选的分离度映射到 [0,1]。

    参数：chosen_score —— 选中候选 cosine；runner_up_score —— 其余候选最高 cosine（None=唯一候选，无歧义）；
      margin_full —— 视作无歧义所需的间距。
    返回：[0,1]。唯一候选→1；间距 ≥ margin_full→1；间距为负（逆检索而选）→0；margin_full<=0 退化为阶跃。
    """
    if runner_up_score is None:
        return 1.0
    if margin_full <= 0:
        return 1.0 if chosen_score >= runner_up_score else 0.0
    return _clamp01((chosen_score - runner_up_score) / margin_full)


def external_confidence(
    chosen_score: float | None,
    runner_up_score: float | None,
    *,
    floor: float,
    ceil: float,
    margin_full: float,
) -> float | None:
    """从检索几何算外部客观置信（绝对贴合度 ∧ 间距，取 min）。

    参数：chosen_score —— 选中候选 cosine（None=无 score 可用，不校准）；runner_up_score —— 其余候选最高 cosine；
      floor/ceil/margin_full —— 校准参数（见 config）。
    返回：[0,1] 外部置信；``chosen_score is None`` → None（调用方据此回退仅用自报，不无端惩罚）。
    """
    if chosen_score is None:
        return None
    return round(min(_abs_conf(chosen_score, floor, ceil),
                     _margin_conf(chosen_score, runner_up_score, margin_full)), 4)


def calibrate(
    llm_confidence: float,
    chosen_score: float | None,
    runner_up_score: float | None,
    *,
    floor: float,
    ceil: float,
    margin_full: float,
) -> tuple[float, float | None]:
    """自报置信 + 外部信号 → 校准后有效置信（保守取 min）。

    参数：llm_confidence —— LLM 自报置信；chosen_score/runner_up_score —— 选中/次优候选 cosine；校准参数同上。
    返回：``(effective, external)``：effective=min(自报, 外部)（外部为 None 时=自报，无 score 不惩罚）；
      external=外部置信（None=无 score）。供调用方把 effective 用于阈值/闸门、保留 external 入审计/benchmark。
    """
    external = external_confidence(chosen_score, runner_up_score,
                                   floor=floor, ceil=ceil, margin_full=margin_full)
    if external is None:
        return llm_confidence, None
    return round(min(llm_confidence, external), 4), external
