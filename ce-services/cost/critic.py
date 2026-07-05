"""Critic Agent v0（M3 §3.1 管线④ · 草表对抗式复核）—— 对构件抽取草表提质疑，不改数据。

定位：判断层第二个 Agent 原语。对「原文 + 抽取草表」做**对抗式三问**，产出结构化质疑清单
（findings）供评审表标注——**只出质疑不出修改**（钉值权在闸和人，Critic 连提案都不给，
只指出「这里可能有问题、依据是原文这句」）。

对抗式三问（v0 范围，组价结果级复核如错套/价格合理性归后续版本——那需要定额数据覆盖先就位）：
  ① missing_item —— 漏项：原文明确出现、草表没有的可计价构件；
  ② weak_feature —— 特征不全：某件缺选码关键特征（强度等级/材料规格/厚度截面），可能致错码；
  ③ quantity_doubt —— 量疑点：草表 Q 与原文数字不符 / 原文有量草表没带上。

入籍守则（与 listing 同规）：
  - 信封进出：``{step:"critic_review", status, result{findings}, provenance}``；
  - 溯源硬校验：每条 finding 的 source_text 必须是原文子串（引用幻觉整条作废、作废出声）；
  - 降级诚实：LLM 失败 → ``status=need_review`` + 空 findings + note——**「无质疑」≠「没问题」**，
    note 必须写明 Critic 未执行，评审表据此显示「复核未运行」而非绿灯；
  - 金标评：``benchmark/critic_eval/``（tools/critic_eval.py，查全率 + 溯源违规=0）；
  - 模型：桶 B 32b（对抗复核是真推理）；``llm_fn`` 可注入。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import requests

from common.config import ORCH_LLM_MODEL_ID, ORCH_LLM_URL
from common.llm import call_qwen3

logger = logging.getLogger("ce-services.cost.critic")

# finding 类型白名单（越界类型作废该条——LLM 不得自创质疑类别）
FINDING_TYPES = ("missing_item", "weak_feature", "quantity_doubt")
# 质疑上限：防复核比草表还长（超限截断出声）
MAX_FINDINGS = 20
MAX_TEXT_CHARS = 6000

_CRITIC_SYSTEM = """\
你是建设工程造价的清单草表**对抗式复核员**。给定「项目特征描述原文」和「已抽取的构件草表」，\
只做一件事：找茬——提出结构化质疑，绝不改数据、绝不补建议值。

三类质疑（type 只能取这三个值）：
- missing_item —— 漏项：原文明确出现、但草表里没有的可计价构件/做法。
- weak_feature —— 特征不全：草表某件的描述缺少清单选码关键特征（强度等级/材料品种/规格厚度/\
现浇预制），可能导致错码。
- quantity_doubt —— 量疑点：草表工程量与原文数字不符，或原文带量而草表没带上。

铁律：
1. 每条质疑必须带 source_text：**逐字摘录**原文中支撑该质疑的片段（禁止改写；给不出原文依据的\
质疑不要提）。
2. item_index：质疑针对草表第几件（0 起）；漏项类与具体件无关则省略该字段。
3. 只质疑**有原文依据**的问题；草表没毛病就返回空列表——不要为了显得尽职硬凑质疑。
4. detail 一句话说清「疑什么、为什么」，面向造价员。
5. 提出 missing_item 前**逐条核对草表**：该构件若已在草表任何一件的描述里出现（含名称变体），\
**绝不判漏项**——把已存在的件报成漏项是最伤信任的误报。

示例：
原文：「外墙MU15多孔砖370厚。屋面SBS改性沥青防水卷材两道。」
草表：[0] MU15多孔砖370厚砖墙（Q=80）
输出：{"findings": [{"type": "missing_item", "detail": "原文含屋面SBS防水卷材做法，草表未列项", \
"source_text": "屋面SBS改性沥青防水卷材两道"}, {"type": "quantity_doubt", "item_index": 0, \
"detail": "草表砖墙带 Q=80，但原文未见 80 对应的工程量数字", "source_text": "外墙MU15多孔砖370厚"}]}

只返回合法 JSON：{"findings": [{"type": "...", "item_index": 0, "detail": "...", "source_text": "..."}]}\
"""


def _critic_user(text: str, items: list[dict[str, Any]]) -> str:
    rows = [f"[{i}] {it.get('feature')}"
            + (f"（Q={it.get('quantity')}）" if it.get("quantity") is not None else "")
            for i, it in enumerate(items)]
    return (f"项目特征描述原文：\n{text}\n\n已抽取构件草表：\n" + "\n".join(rows)
            + "\n\n对抗式复核，只返回合法 JSON。\n\n/no_think")


def _ngrams(s: str, n: int = 4) -> set[str]:
    """字符 4-gram 集（短串整串兜底）——中文免分词的重叠度基元。"""
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def _is_false_missing(source_text: str, items: list[dict[str, Any]]) -> bool:
    """假漏项判定（代码侧第三重保险）：missing_item 的原文摘录若与草表某件 feature 高度重合，
    说明该构件已在草表——判漏项即假漏项，作废。

    07-05 实测：prompt 铁律（「提漏项前逐条核对草表」）治不住 32b 的假漏项——「对照清单查重」
    是代码比 LLM 可靠的活，不该指望提示词。判法：feature 的 4-gram 被 source_text 覆盖 ≥70%
    即视为同一构件（阈值防「现浇钢筋混凝土」类通用词串误杀真漏项——金标 C1 真漏项护着回归）。
    """
    src_grams = _ngrams(source_text)
    for it in items:
        feat = str(it.get("feature") or "")
        if not feat:
            continue
        if len(feat) < 4:
            if feat in source_text:
                return True
            continue
        feat_grams = _ngrams(feat)
        if feat_grams and len(feat_grams & src_grams) / len(feat_grams) >= 0.7:
            return True
    return False


def _envelope(status: str, findings: list[dict[str, Any]], note: str | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {
        "step": "critic_review", "status": status,
        "result": {"findings": findings},
        "provenance": {"source_type": "user_input",
                       "source_ref": "Critic 对抗复核（质疑基于用户原文，逐条带原文摘录）"},
    }
    if note:
        env["note"] = note
    return env


def review_extraction(
    text: str,
    items: list[dict[str, Any]],
    llm_url: str = ORCH_LLM_URL,
    model_id: str = ORCH_LLM_MODEL_ID,
    llm_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对抽取草表做对抗式复核 → 质疑信封（只质疑不修改）。

    参数：text —— 原文；items —— 抽取草表（listing 信封的 result.items）；
      llm_fn —— 可注入 ``(system, user) -> dict``（stub 测试 / 模型对比）。
    返回：信封——``ok``＝复核已执行（findings 可为空=未发现问题）；``need_review``＝Critic
      未能执行（LLM 失败/输出非法/空输入），**评审表须显示「复核未运行」而非绿灯**。
    校验：type ∈ 白名单、source_text ⊆ 原文、item_index ∈ 草表范围——任一不过整条作废并计数出声。
    """
    text = (text or "").strip()
    if not text or not items:
        return _envelope("need_review", [], note="原文或草表为空，复核未执行")
    sent = text[:MAX_TEXT_CHARS]

    _call = llm_fn or (lambda s, u: call_qwen3(s, u, llm_url, model_id, temperature=0.0))
    try:
        raw = _call(_CRITIC_SYSTEM, _critic_user(sent, items))
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Critic 复核 LLM 不可靠 → need_review（复核未执行）：%s", exc)
        return _envelope("need_review", [], note=f"复核未执行（LLM 不可用/输出非法）：{exc}")

    raw_findings = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(raw_findings, list):
        return _envelope("need_review", [], note="复核输出缺 findings 列表，复核未执行")

    findings: list[dict[str, Any]] = []
    dropped = 0
    for entry in raw_findings[:MAX_FINDINGS]:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        ftype = entry.get("type")
        detail = str(entry.get("detail") or "").strip()
        source = str(entry.get("source_text") or "").strip()
        # 三重校验：类型白名单 / 溯源子串 / 件索引范围（LLM 不得造类别、造引文、指向不存在的件）
        if ftype not in FINDING_TYPES or not detail or not source or source not in sent:
            dropped += 1
            continue
        # 第四重（仅 missing_item）：假漏项代码判——构件已在草表却被判漏项，作废（prompt 治不住）
        if ftype == "missing_item" and _is_false_missing(source, items):
            dropped += 1
            continue
        finding: dict[str, Any] = {"type": ftype, "detail": detail, "source_text": source}
        idx = entry.get("item_index")
        if isinstance(idx, int) and 0 <= idx < len(items):
            finding["item_index"] = idx
        elif idx is not None:  # 给了越界索引：保留质疑但去掉指向（不指错件）
            dropped += 0  # 不作废整条——质疑本身可能有效，仅索引不可信
        findings.append(finding)

    note = None
    if dropped:
        note = f"{dropped} 条质疑未过校验已作废（类型越界/无原文依据/字段缺失）"
    if len(raw_findings) > MAX_FINDINGS:
        note = ((note + "；") if note else "") + f"质疑超上限 {MAX_FINDINGS} 条已截断"
    return _envelope("ok", findings, note=note)
