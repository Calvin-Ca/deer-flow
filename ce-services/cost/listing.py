"""列清单解析器（M2 §3.1 管线① · v0 抽取式）—— 设计说明/多构件描述 → 构件条目信封。

定位：批量组价主线的第一块砖。用户拿「项目特征描述/设计说明」起步（PRD 起点场景），本模块把
整段文本抽成**逐件自包含**的构件条目 ``items[]``，供编排器批量点火多构件 HITL 会话
（``session.start(features=[...])``——图的 items[] 外层循环是现成的）。

入籍守则（架构文档 §3 判断层同规）：
  - **契约进、信封出**：输入纯文本，输出 ``{step, status, result{items}, provenance}`` 信封；
    每件带 ``source_text``（原文摘录，provenance 是字段不是散文）。
  - **只抽不造**（红线 1 同款）：只抽原文出现的构件，代码侧再校验 source_text ⊆ 原文，
    对不上的条目作废（LLM 引用幻觉不入账）。
  - **金标评**：``benchmark/listing_eval/`` 构件抽取召回（tools/listing_eval.py，G2 门 ≥85%）。
  - **降级兜**：LLM 失败/非法输出 → ``status=need_review`` + 空 items（诚实报无法解析，
    交上层引导用户手工列项；**不**把整段硬当单构件产垃圾匹配）。
  - v0 为**抽取式单次调用**（M2）；M3 升级迭代式（分段读→检索验证→自查遗漏）时本信封契约不变。

模型：桶 B 32b（ORCH_LLM_*，未部署成对回落 8b）——抽错/漏件影响整单，非 2s 直配路径可承受延迟。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import requests

import re

from common.config import ORCH_LLM_MODEL_ID, ORCH_LLM_URL
from common.llm import call_qwen3

logger = logging.getLogger("ce-services.cost.listing")

# source_text 里的独立数字 token（整数/小数）——量校验用精确匹配，防子串漏洞
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# 抽取上限：防超长说明爆量（>30 件的真实项目应分批；截断时如实标注，不静默丢）。
MAX_ITEMS = 30
# 送 LLM 的原文上限（字符）：超长截断并标注（v0 单次调用窗口内做事；分段读归 M3 迭代式）。
MAX_TEXT_CHARS = 6000

_EXTRACT_SYSTEM = """\
你是建设工程造价的构件抽取器。给定一段项目特征描述/设计说明，把其中**每一个需要单独计价的构件/做法**\
抽成独立条目，供逐件清单编码匹配。

铁律：
1. **只抽原文明确出现的构件，绝不臆造**；原文没有的材料/强度/尺寸不得脑补。
2. 每条 feature 必须**自包含**：把该构件在原文中的关键特征（强度等级/材料/规格尺寸/现浇预制/部位）\
拼进一句描述，能脱离原文单独用于清单选码。
3. 每条带 source_text：**逐字摘录**原文中支撑该条目的片段（用于溯源校验，禁止改写）。
4. 同一构件不重复抽；纯说明性文字（工期/责任/验收条款）不是构件，不抽。
5. quantity 只在**原文数字明确属于该构件**时填写，并把含该数字的原文片段一并摘进 source_text；\
数字属于哪个构件拿不准时**宁可不填**——错量比缺量有害。没有则省略字段，**不得编造数量**。
6. 砌筑砂浆/混凝土强度/钢筋牌号/防水做法等是构件的**特征不是构件**：**必须写进所属构件的 \
feature 里**（如砖墙条目应写成「MU10标准砖240厚实心砖墙M5水泥砂浆砌筑」），绝不单独成条，\
也**不许丢弃**——特征丢了会导致清单选码错误。
7. 钢筋制作安装可单列为独立计价条目，但 feature 必须带部位与语境\
（如「柱内HRB400钢筋制作安装」，不得只写「HRB400钢筋」）。

示例（注意三点：砂浆**并入**墙的 feature 而不是消失或单列；含数字的量句**必须摘进** source_text \
才能填 quantity；原文无量的构件不带 quantity 字段）：
原文：「外墙为MU15多孔砖370厚，M7.5混合砂浆砌筑，约200m²。梁下设C20素混凝土垫层。」
输出：{"items": [{"feature": "MU15多孔砖370厚砖墙M7.5混合砂浆砌筑", \
"source_text": "外墙为MU15多孔砖370厚，M7.5混合砂浆砌筑，约200m²", "quantity": 200, "unit": "m2"}, \
{"feature": "梁下C20素混凝土垫层", "source_text": "梁下设C20素混凝土垫层"}]}

只返回合法 JSON，不输出任何 JSON 以外的文字：
{"items": [{"feature": "自包含构件描述", "source_text": "原文逐字摘录（含量句才可填量）", "quantity": 120, "unit": "m3"}]}\
"""


def _extract_user(text: str) -> str:
    return f"项目特征描述/设计说明：\n{text}\n\n抽取全部可计价构件条目，只返回合法 JSON。\n\n/no_think"


def _envelope(status: str, items: list[dict[str, Any]], note: str | None = None,
              truncated: bool = False) -> dict[str, Any]:
    """组装抽取信封（形状对齐 provenance 信封族：step/status/result/provenance）。"""
    prov: dict[str, Any] = {"source_type": "user_input",
                            "source_ref": "构件抽取自用户提供的项目特征描述（listing v0 抽取式）"}
    if truncated:
        prov["note"] = f"原文超 {MAX_TEXT_CHARS} 字符已截断，后段构件可能未抽取（分批提交可补全）"
    env: dict[str, Any] = {"step": "extract_components", "status": status,
                           "result": {"items": items}, "provenance": prov}
    if note:
        env["note"] = note
    return env


def extract_components(
    text: str,
    llm_url: str = ORCH_LLM_URL,
    model_id: str = ORCH_LLM_MODEL_ID,
    llm_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从设计说明/多构件描述抽取可计价构件条目（v0 单次抽取式）。

    参数：text —— 原文；llm_url/model_id —— 桶 B 32b（可覆盖）；llm_fn —— 可注入
      ``(system, user) -> dict``（单测 stub / benchmark 换模型对比）。
    返回：信封 ``{step, status, result{items[{feature, source_text, quantity?, unit?}]}, provenance}``：
      - ``ok`` —— 抽到 ≥1 件且全部通过溯源校验；
      - ``need_review`` —— LLM 失败/非法输出/0 件/全部溯源失败 → 空 items ＋ note（诚实报，不硬拆）；
      红线：source_text 必须是原文子串（引用幻觉作废该条）；quantity 非正数作废该字段（不编量）。
    """
    text = (text or "").strip()
    if not text:
        return _envelope("need_review", [], note="输入为空，无可抽取内容")
    truncated = len(text) > MAX_TEXT_CHARS
    sent = text[:MAX_TEXT_CHARS]

    _call = llm_fn or (lambda s, u: call_qwen3(s, u, llm_url, model_id, temperature=0.0))
    try:
        raw = _call(_EXTRACT_SYSTEM, _extract_user(sent))
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.warning("构件抽取 LLM 不可靠 → need_review：%s", exc)
        return _envelope("need_review", [], note=f"构件抽取失败（LLM 不可用/输出非法）：{exc}",
                         truncated=truncated)

    raw_items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(raw_items, list):
        return _envelope("need_review", [], note="构件抽取输出缺 items 列表", truncated=truncated)

    items: list[dict[str, Any]] = []
    dropped = 0
    for entry in raw_items[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        feature = str(entry.get("feature") or "").strip()
        source = str(entry.get("source_text") or "").strip()
        # 红线：只抽不造——source_text 必须能在原文找到（引用幻觉作废整条，不静默采信）。
        if not feature or not source or source not in sent:
            dropped += 1
            continue
        item: dict[str, Any] = {"feature": feature, "source_text": source}
        qty = entry.get("quantity")
        # 不编量双保险：非正数/非数值作废；且量必须**恰好等于** source_text 里的某个独立数字 token
        # （挡编量与串量——实测 32b 会把砖墙的 350 串给砌块墙、给无量构件编 Q=1；
        #   子串匹配有漏洞："1" in "板厚120mm" 为真，Q=1 能钻过，故用 token 精确匹配）。
        if (isinstance(qty, (int, float)) and qty > 0
                and f"{qty:g}" in _NUM_RE.findall(source)):
            item["quantity"] = float(qty)
            if entry.get("unit"):
                item["unit"] = str(entry["unit"])
        items.append(item)

    if not items:
        return _envelope("need_review", [],
                         note=f"未抽取到可溯源的构件条目（LLM 产出 {len(raw_items)} 条均未过校验）"
                         if raw_items else "原文中未识别出可计价构件",
                         truncated=truncated)

    note = None
    if dropped:
        note = f"{dropped} 条抽取结果未过溯源校验已作废（source_text 不在原文/字段缺失）"
    if len(raw_items) > MAX_ITEMS:
        note = ((note + "；") if note else "") + f"超出单批上限 {MAX_ITEMS} 件，其余未处理（请分批）"
    return _envelope("ok", items, note=note, truncated=truncated)


# ── v1 增量补抽原语（M3 多 agent 对比实验：P2 自查 / P3 Critic 定向修正共用）──

_REFINE_SYSTEM = """\
你是建设工程造价的构件**补抽器**。给定原文、已抽取的草表、以及（可能有的）复核质疑，\
只做一件事：找出**草表里没有、原文里有**的可计价构件，补成新增条目。

铁律（与首轮抽取同规）：
1. 只补原文明确出现的构件；feature 自包含；source_text 逐字摘录原文。
2. **绝不重复草表已有的构件**（含名称变体）；没有可补的就返回空列表——不要硬凑。
3. 不修改、不删除已有条目——你只产增量。
4. quantity 只在数字明确属于该构件且量句已摘进 source_text 时填，否则省略。

只返回合法 JSON：{"items": [{"feature": "...", "source_text": "...", "quantity": 120, "unit": "m3"}]}\
"""


def _refine_user(text: str, items: list[dict[str, Any]], hints: list[str] | None) -> str:
    rows = [f"[{i}] {it.get('feature')}" for i, it in enumerate(items)]
    hint_block = ""
    if hints:
        hint_block = "\n复核质疑（重点核对这些线索）：\n" + "\n".join(f"- {h}" for h in hints)
    return (f"原文：\n{text}\n\n已抽取草表：\n" + "\n".join(rows) + hint_block
            + "\n\n对照原文找出草表遗漏的可计价构件（没有则 items 为空），只返回合法 JSON。\n\n/no_think")


def refine_missing(
    text: str,
    base_items: list[dict[str, Any]],
    hints: list[str] | None = None,
    llm_url: str = ORCH_LLM_URL,
    model_id: str = ORCH_LLM_MODEL_ID,
    llm_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """增量补抽（v1 原语）：对照原文找草表漏项 → 校验后合并 → 新信封。

    参数：text —— 原文；base_items —— 首轮草表；hints —— 定向线索（P3 传 Critic 的
      missing_item 质疑文本；P2 自查不传）；llm_fn —— 可注入。
    返回：信封 ``result.items`` = base + 通过校验的新增件；``result.added`` = 新增数。
      新增件三重校验：溯源子串 / 量数字在摘录 token 中（同首轮）/ **4-gram 判重**（与既有件
      重叠 ≥70% 视为重复丢弃——补抽器绝不许靠复读已有件刷增量）。失败降级：LLM 挂 →
      原表原样返回（added=0 + note），补抽失败绝不损失首轮成果。
    """
    from cost.critic import _ngrams  # 同包私有复用（4-gram 判重与 critic 假漏项判同源，防漂移）

    text = (text or "").strip()
    sent = text[:MAX_TEXT_CHARS]
    base = [dict(it) for it in (base_items or [])]

    _call = llm_fn or (lambda s, u: call_qwen3(s, u, llm_url, model_id, temperature=0.0))
    try:
        raw = _call(_REFINE_SYSTEM, _refine_user(sent, base, hints))
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.warning("补抽 LLM 不可靠 → 保留首轮成果：%s", exc)
        env = _envelope("ok", base, note=f"补抽未执行（LLM 不可用）：{exc}")
        env["result"]["added"] = 0
        return env

    raw_items = raw.get("items") if isinstance(raw, dict) else None
    added = dropped = 0
    if isinstance(raw_items, list):
        for entry in raw_items[:MAX_ITEMS]:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            feature = str(entry.get("feature") or "").strip()
            source = str(entry.get("source_text") or "").strip()
            if not feature or not source or source not in sent:
                dropped += 1
                continue
            # 4-gram 判重：与既有件（含本轮已收的新增件）高重叠 = 复读，丢弃
            fg = _ngrams(feature)
            if fg and any(
                len(fg & _ngrams(str(b.get("feature") or ""))) / len(fg) >= 0.7 for b in base
            ):
                dropped += 1
                continue
            item: dict[str, Any] = {"feature": feature, "source_text": source}
            qty = entry.get("quantity")
            if (isinstance(qty, (int, float)) and qty > 0
                    and f"{qty:g}" in _NUM_RE.findall(source)):
                item["quantity"] = float(qty)
                if entry.get("unit"):
                    item["unit"] = str(entry["unit"])
            base.append(item)
            added += 1

    note = None
    if dropped:
        note = f"{dropped} 条补抽结果未过校验/判重已作废"
    env = _envelope("ok", base, note=note)
    env["result"]["added"] = added
    return env
