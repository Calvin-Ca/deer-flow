"""
阶段 3.2：评测集生成

基于条文库生成 400 题，按类型分策略生成：
  single_clause  120  单条文查询（优先强制性/含表格条文）
  cross_clause   100  跨条文推理（基于 refs 对）
  calculation     80  参数查表/计算（要求给出 gold_values）
  clause_verify   60  条款引用真伪（40 真 + 20 诱导错误）
  refusal         40  该拒答（超范围/需现场数据/规范冲突）

输出：data/eval/evalset_v1.jsonl

运行：
  python -m src.eval.gen_evalset --smoke          # 各类型各 5 条
  python -m src.eval.gen_evalset --workers 8      # 全量
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/eval"

sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, print_cost_summary
from src.synth.group_a import convert_text_tables
from src.utils import jsonx

# 出题模型。默认 qwen-max（DashScope API），**刻意不同于合成模型**
# Qwen3-32B-AWQ —— 同模型出题会导致题目与训练样本撞车（触发铁律 3 泄漏检查）
# 和风格耦合（题目风格贴近 B/C/D 而非 A 组模板体，系统性放大 A→B 的提升）。
# 同厂不同代的独立性弱于跨厂，须写进 EXPERIMENT.md 的已知局限。
_MODEL = os.getenv("CE_EVAL_MODEL", "qwen-max")
# 思考模式已由 llm.call() 统一关闭（见 _build_extra_body），此处无需再传
_EXTRA: dict = {}

# ── 出题模型分发 ──────────────────────────────────────────────────────────
# 评测集默认换厂商出题，理由见 src/utils/claude.py 模块 docstring：
# B/C/D 由 Qwen3-32B 合成，同模型出题会导致题目撞车（触发铁律 3 泄漏检查）
# 与风格耦合（题目风格贴近 B/C/D 而非 A 组模板体，系统性放大 A→B 的提升）。
PROVIDER = os.getenv("CE_EVAL_PROVIDER", "openai_compat")


def _gen(prompt: str, system: str, max_tokens: int, sample_id: str,
         temperature: float = 0.5, seed: int = 42) -> str:
    """按 PROVIDER 分发一次出题调用。

    两个 provider 的参数差异被这层吸收，各生成函数不必分别处理：
      openai_compat —— 走 llm.call 的 OpenAI 兼容端点，覆盖 DashScope 的
                       qwen-max 与本机 vLLM 两种情形，保留 temperature/seed
      claude        —— 走 Anthropic 官方 SDK。不接受 temperature/seed
                       （Sonnet 5 对非默认采样参数返回 400）；思考 token 与正文
                       共用 max_tokens 预算，故按 4 倍留余量

    Args:
        prompt:      用户轮内容
        system:      系统提示
        max_tokens:  正文所需的 token 上限（claude 分支自行放大）
        sample_id:   样本 ID，用于失败留痕
        temperature: 采样温度。**必须逐题透传**——拒答题不依赖条文，
                     三类 prompt 各自固定，只靠 seed/temperature 拉开差异；
                     早先在此写死 seed=42 导致同类拒答题输出完全相同，
                     去重后 40 条只剩 29 条。
        seed:        随机种子，同上

    Returns:
        模型输出文本
    """
    if PROVIDER == "claude":
        from src.utils import claude
        return claude.call(prompt, system=system,
                           max_tokens=max_tokens * 4, sample_id=sample_id)
    return llm_call(prompt, system=system, model=_MODEL,
                    max_tokens=max_tokens, temperature=temperature, seed=seed,
                    sample_id=sample_id, extra_body=_EXTRA)


_FAILED_DIR = _ROOT / "data/interim/failed"


def _log_fail(kind: str, sample_id: str, reason: str, detail: str = "") -> None:
    """记录出题失败（CLAUDE.md §6.6：失败要留痕，不得静默丢弃）。

    原实现各生成函数一律 `except Exception: return None`，400 题里失败 72 题
    却查不出原因，只能靠事后单独复现才定位到 LaTeX 转义问题。

    Args:
        kind:      题型
        sample_id: 样本 ID
        reason:    失败原因代码
        detail:    模型原始输出片段等

    Returns:
        None（追加写入 data/interim/failed/gen_evalset_failed.jsonl）
    """
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)
    with open(_FAILED_DIR / "gen_evalset_failed.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "sample_id": sample_id,
                            "reason": reason, "detail": detail[:400]},
                           ensure_ascii=False) + "\n")


# ── 条文加载 ──────────────────────────────────────────────────────────────

def _load_clauses(path: Path) -> list[dict]:
    clauses = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            clauses.append(json.loads(line))
    return clauses


def _clause_map(clauses: list[dict]) -> dict[str, dict]:
    return {c["clause_id"]: c for c in clauses}


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _sid(prefix: str, *parts: str) -> str:
    h = hashlib.md5("|".join(parts).encode()).hexdigest()[:8]
    return f"eval_{prefix}_{h}"


def _parse_qa(raw: str) -> dict | None:
    """从模型输出中提取题目 JSON。

    注意字段名是 **gold_answer** 而非 answer——所有 prompt 的输出格式都写的
    gold_answer。原实现校验 obj.get("answer")，恒为 None，导致本生成器
    对任何输入都返回 None、产出零条题目且不报错；评测集因此改走了硬编码路线。

    Args:
        raw: 模型原始输出

    Returns:
        题目字典；缺 question 或 gold_answer、或 JSON 不可解析时返回 None
    """
    obj = jsonx.extract(raw, kind="object")
    if not isinstance(obj, dict):
        return None
    if not obj.get("question") or not obj.get("gold_answer"):
        return None
    return obj


def _dedup(items: list[dict]) -> list[dict]:
    """落盘前去重：删掉完全相同的题，并保证 id 唯一。

    选题层已改为无放回抽样，但仍需这道防线——同一条文可能被不同题型选中，
    模型也可能对不同条文产出雷同题面。两种情形要分开处理：
      · **题面完全相同** → 真重复题，直接丢弃（保留先到的一条）
      · **题面不同但 id 相同** → 纯哈希碰撞（_sid 以 clause_id 为输入），
        两题都有效，给后到者加序号后缀

    id 重复的危害是隐蔽的：阶段 5 按 id 索引结果，撞 id 会让后一条静默覆盖前一条，
    评测题数悄悄变少而指标看起来正常。

    Args:
        items: 待落盘的题目列表

    Returns:
        去重并保证 id 唯一后的列表（保持原有顺序）
    """
    seen_q: set[str] = set()
    seen_id: set[str] = set()
    out: list[dict] = []
    dropped = renamed = 0
    for it in items:
        q = it["question"].strip()
        if q in seen_q:
            dropped += 1
            continue
        seen_q.add(q)
        if it["id"] in seen_id:
            base, k = it["id"], 2
            while f"{base}_{k}" in seen_id:
                k += 1
            it["id"] = f"{base}_{k}"
            renamed += 1
        seen_id.add(it["id"])
        out.append(it)
    if dropped or renamed:
        print(f"[gen_evalset] 去重：丢弃重复题 {dropped} 条，id 碰撞重命名 {renamed} 条")
    return out


def _extract_nums(text: str) -> list[str]:
    """从条文文本中提取数值字符串作为 gold_values 候选。"""
    nums = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    # 过滤掉条款号里的数字（如 8.2.1 → 8, 2, 1）
    return [n for n in nums if float(n) > 0.05][:8]


def _verify_gold_values(values: list, clause_text: str) -> list[str]:
    """只保留能在来源条文中找到的 gold_values（金标回锚）。

    这是硬编码版评测集翻车的根源：金标由模型/人凭记忆写出、从不回查条文，
    实测 463 个 gold_values 有 350 个（76%）在对应条文里根本不存在。
    尺子本身错了，六个模型全被错误地扣分或加分，且指标看起来完全正常。

    适用范围：查表类题型（single_clause / cross_clause / clause_verify）的
    gold_values 应当是条文里的**原值**，可逐一回查。
    **计算题除外**——其答案是推导出来的，本就不会出现在条文中，见 gen_calculation。

    Args:
        values:      模型给出的 gold_values
        clause_text: 来源条文原文（多条时拼接）

    Returns:
        经回查确认存在于条文中的值；全部落空时返回空列表
    """
    out = []
    for v in values or []:
        s = str(v).strip()
        if not s or s not in clause_text:
            continue
        if not _is_value_like(s):
            continue
        out.append(s)
    return out


# 金标值的形态约束：阶段 5.4 是**数值精确匹配**判分，字段里必须是可比对的值
# （"14.3" / "C30" / "0.25%" / "HRB400"），不能是描述性句子。
# 实测 qwen-max 会把整条条文当成 gold_values 塞回来——它能通过"存在于条文中"
# 的回锚（整句确实在条文里），却让该题在 5.4 无法判分。prompt 已明令禁止，
# 此处是代码层兜底：prompt 靠自觉，过滤靠代码。
_MAX_VALUE_LEN = 20


# 规范里大量用中文数字表达分级：抗震等级「一、二、三、四」级，
# 建筑类别「甲、乙、丙、丁」类，场地「I~IV」类。这些都是可逐字符比对的合法金标，
# 仅要求「含阿拉伯数字」会把它们全部误伤——实测「抗震等级应为三」整条金标被剔空。
_RE_GRADE_VALUE = re.compile(r"^[一二三四五六七八九十甲乙丙丁ⅠⅡⅢⅣIV]+\s*[级类等]?$")


def _is_value_like(s: str) -> bool:
    """判断字符串是否为可用于精确匹配的「值」而非描述性文字。

    合格的两类：
      1. 含阿拉伯数字且足够短 —— "14.3" / "C30" / "0.25%" / "GB50153-2008"
      2. 中文数字或字母表示的规范分级 —— "三级" / "甲类" / "Ⅱ类"

    Args:
        s: 待判定的候选金标值

    Returns:
        True 表示形态合格
    """
    if len(s) > _MAX_VALUE_LEN:
        return False
    return any(ch.isdigit() for ch in s) or bool(_RE_GRADE_VALUE.match(s))


# ═══════════════════════════════════════════════════════════════════════════
# 各类型生成函数
# ═══════════════════════════════════════════════════════════════════════════

# ── single_clause ─────────────────────────────────────────────────────────

_SINGLE_SYSTEM = "你是建筑结构工程考试命题专家。基于提供的规范条文，出一道考查工程人员对该条文掌握情况的单选或简答题。"

_SINGLE_PROMPT = """以下是《{std_name}》（{std_code}）第{clause_no}条：

{text}

请出一道评测题，要求：
1. 考查该条文的核心数值、限值或要求，有明确唯一正确答案
2. **必须是简答题，严禁出成选择题**——不得给出 A/B/C/D 选项，不得让答题者从候选中挑选
3. 问题具体（给定工程场景和参数），不能含糊
4. 若条文含表格，优先考查表格中的具体数值
5. gold_answer 要包含条款号引用

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "...", "gold_values": ["数值1", "数值2"]}}

gold_values **只填数值本身**，如 "14.3"、"C30"、"0.25"、"55"。
严禁填入整句话或描述性文字——该字段用于数值精确匹配判分，填句子会使该题无法判分。
答案中没有明确数值时填空列表。"""


def gen_single_clause(
    clause: dict,
    seed: int = 42,
) -> dict | None:
    text = convert_text_tables(clause["text"])
    prompt = _SINGLE_PROMPT.format(
        std_name=clause["standard_name"],
        std_code=clause["standard_code"],
        clause_no=clause["clause_no"],
        text=text,
    )
    _KIND, _SID = "single_clause", clause["clause_id"]
    try:
        raw = _gen(
            prompt, _SINGLE_SYSTEM, 800,
            f"eval_single_{clause['clause_id']}", 0.5, seed,
        )
    except Exception as exc:
        _log_fail(_KIND, _SID, "api_error", f"{type(exc).__name__}: {exc}")
        return None

    qa = _parse_qa(raw)
    if not qa:
        _log_fail(_KIND, _SID, "parse_failed", raw)
        return None

    return {
        "id": _sid("sc", clause["clause_id"]),
        "type": "single_clause",
        "question": qa["question"],
        "gold_clauses": [clause["clause_id"]],
        # 只保留能在条文中回查到的值；模型未给或全部落空时不再用
        # _extract_nums 兜底——那会抽出与问题无关的数值，制造假金标
        "gold_values": _verify_gold_values(qa.get("gold_values"), clause["text"]),
        "gold_verified": True,
        "gold_answer": qa.get("gold_answer", ""),
        "should_refuse": False,
        "source": f"AI生成/{clause['standard_code']}",
        "difficulty": "hard" if clause.get("is_mandatory") else "medium",
    }


# ── cross_clause ──────────────────────────────────────────────────────────

_CROSS_SYSTEM = "你是建筑结构工程考试命题专家。基于两条相互引用的规范条文，出一道必须同时参考两条才能回答的综合题。"

_CROSS_PROMPT = """以下是两条相互关联的规范条文：

【条文A】{std_a}第{no_a}条
{text_a}

【条文B】{std_b}第{no_b}条
{text_b}

请出一道评测题，要求：
1. 问题必须同时用到两条条文才能完整作答
2. **必须是简答题，严禁出成选择题**——不得给出 A/B/C/D 选项
3. 给定具体工程参数场景
4. gold_answer 引用两个条款号，给出完整结论
5. gold_values 只填**两条条文原文中出现的关键取值**（限值、设计值、系数等），
   如 "0.25"、"C30"、"1.2"；不要填你自己算出来的结果——计算结果不在条文里，
   无法回查校验，会被判为无效金标而丢弃。答案不依赖条文原值时填空列表。

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "...", "gold_values": ["数值1"]}}"""


def gen_cross_clause(
    clause_a: dict,
    clause_b: dict,
    seed: int = 42,
) -> dict | None:
    text_a = convert_text_tables(clause_a["text"])
    text_b = convert_text_tables(clause_b["text"])
    prompt = _CROSS_PROMPT.format(
        std_a=clause_a["standard_code"], no_a=clause_a["clause_no"], text_a=text_a,
        std_b=clause_b["standard_code"], no_b=clause_b["clause_no"], text_b=text_b,
    )
    _KIND, _SID = "cross_clause", f"{clause_a['clause_id']}+{clause_b['clause_id']}"
    try:
        raw = _gen(
            prompt, _CROSS_SYSTEM, 900,
            f"eval_cross_{clause_a['clause_id']}_{clause_b['clause_id']}", 0.5, seed,
        )
    except Exception as exc:
        _log_fail(_KIND, _SID, "api_error", f"{type(exc).__name__}: {exc}")
        return None

    qa = _parse_qa(raw)
    if not qa:
        _log_fail(_KIND, _SID, "parse_failed", raw)
        return None

    return {
        "id": _sid("cc", clause_a["clause_id"], clause_b["clause_id"]),
        "type": "cross_clause",
        "question": qa["question"],
        "gold_clauses": [clause_a["clause_id"], clause_b["clause_id"]],
        "gold_values": _verify_gold_values(
            qa.get("gold_values"), clause_a["text"] + "\n" + clause_b["text"]),
        "gold_verified": True,
        "gold_answer": qa.get("gold_answer", ""),
        "should_refuse": False,
        "source": f"AI生成/{clause_a['standard_code']}×{clause_b['standard_code']}",
        "difficulty": "hard",
    }


# ── calculation ───────────────────────────────────────────────────────────

_CALC_SYSTEM = "你是建筑结构工程考试命题专家。出一道给定工程参数、要求查规范表格或套公式得出具体数值的计算/查表题。"

_CALC_PROMPT = """以下是《{std_name}》（{std_code}）第{clause_no}条（含数值限值或计算规定）：

{text}

请出一道计算或查表题，要求：
1. 给定具体工程参数（截面尺寸、材料强度、荷载值、抗震等级等）
2. **必须是简答题，严禁出成选择题**——不得给出 A/B/C/D 选项
3. 要求查出或计算某一关键结果（限值/设计值/是否满足要求）
4. gold_values 必须填写正确答案中的关键数值，**只填数值本身**（如 "14.3"、"360"），
   严禁填入整句话——该字段用于数值精确匹配判分
5. 答案步骤清晰

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "...", "gold_values": ["具体数值"]}}"""


def gen_calculation(
    clause: dict,
    seed: int = 42,
) -> dict | None:
    text = convert_text_tables(clause["text"])
    prompt = _CALC_PROMPT.format(
        std_name=clause["standard_name"],
        std_code=clause["standard_code"],
        clause_no=clause["clause_no"],
        text=text,
    )
    _KIND, _SID = "calculation", clause["clause_id"]
    try:
        raw = _gen(
            prompt, _CALC_SYSTEM, 1000,
            f"eval_calc_{clause['clause_id']}", 0.4, seed,
        )
    except Exception:
        return None

    qa = _parse_qa(raw)
    if not qa or not qa.get("gold_values"):
        return None  # 计算题必须有 gold_values

    return {
        "id": _sid("ca", clause["clause_id"]),
        "type": "calculation",
        "question": qa["question"],
        "gold_clauses": [clause["clause_id"]],
        # 计算题的答案由推导得出，本就不出现在条文中，无法像查表题那样回锚。
        # 如实标记 gold_verified=False：5.4 数值判分对这批题的正确性
        # 依赖出题模型的算术，属已知薄弱环节，须写进报告局限。
        "gold_values": qa["gold_values"],
        "gold_verified": False,
        "gold_answer": qa.get("gold_answer", ""),
        "should_refuse": False,
        "source": f"AI生成/{clause['standard_code']}",
        "difficulty": "hard",
    }


# ── clause_verify ─────────────────────────────────────────────────────────

_VERIFY_SYSTEM = "你是建筑结构工程考试命题专家，专注于出考查条款引用真伪的辨析题。"

_VERIFY_TRUE_PROMPT = """以下是《{std_name}》（{std_code}）第{clause_no}条：

{text}

请出一道辨析题：给出一个引用该条款的说法（正确或有轻微篡改），
让答题者判断是否正确，并给出正确说法。

输出格式（严格JSON）：
{{"question": "有人说：……这个说法对吗？", "gold_answer": "...", "is_trap": false或true, "gold_values": []}}

is_trap=true 表示题目中的说法故意包含错误（数值篡改/张冠李戴），is_trap=false 表示说法正确。
各生成一半，本次生成 is_trap={is_trap}。"""


def gen_clause_verify(
    clause: dict,
    is_trap: bool,
    seed: int = 42,
) -> dict | None:
    text = convert_text_tables(clause["text"])
    prompt = _VERIFY_TRUE_PROMPT.format(
        std_name=clause["standard_name"],
        std_code=clause["standard_code"],
        clause_no=clause["clause_no"],
        text=text,
        is_trap=is_trap,
    )
    _KIND, _SID = "clause_verify", clause["clause_id"]
    try:
        raw = _gen(
            prompt, _VERIFY_SYSTEM, 700,
            f"eval_verify_{clause['clause_id']}_{is_trap}", 0.5, seed,
        )
    except Exception as exc:
        _log_fail(_KIND, _SID, "api_error", f"{type(exc).__name__}: {exc}")
        return None

    qa = _parse_qa(raw)
    if not qa:
        _log_fail(_KIND, _SID, "parse_failed", raw)
        return None

    return {
        "id": _sid("cv", clause["clause_id"], str(is_trap)),
        "type": "clause_verify",
        "question": qa["question"],
        "gold_clauses": [clause["clause_id"]],
        "gold_values": _verify_gold_values(qa.get("gold_values"), clause["text"]),
        "gold_verified": True,
        "gold_answer": qa.get("gold_answer", ""),
        "should_refuse": False,
        "is_trap": is_trap,
        "source": f"AI生成/{clause['standard_code']}",
        "difficulty": "medium",
    }


# ── refusal ───────────────────────────────────────────────────────────────

_REFUSAL_SYSTEM = "你是建筑结构工程考试命题专家，出需要拒答的题目：这些问题超出规范直接回答范围，正确做法是解释原因并拒绝给出确定答案。"

_REFUSAL_CONFIGS = [
    {
        "type": "beyond_scope",
        "quota_ratio": 0.35,
        "prompt": """基于结构工程背景，出一道需要工程判断、规范无法直接给出答案的问题。
场景示例：方案选择、地基处理方案比选、特殊构造做法取舍等。

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "（说明规范只给原则/限值，需结合项目实际由结构工程师判断，建议...）"}}""",
    },
    {
        "type": "needs_field_data",
        "quota_ratio": 0.35,
        "prompt": """出一道需要现场检测数据才能判断的题目。
场景：裂缝是否超标、构件是否满足承载力、既有建筑是否安全等。

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "（说明需要哪些现场数据/检测报告，建议委托有资质机构评估）"}}""",
    },
    {
        "type": "standard_conflict",
        "quota_ratio": 0.30,
        "prompt": """出一道涉及国标与地方标准（或新旧规范）差异、无法单凭一本规范给出确定答案的题目。

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "（说明存在规范差异，指出适用原则，建议由设计院/审图机构裁定）"}}""",
    },
]


def gen_refusal(
    cfg: dict,
    idx: int,
    seed: int = 42,
) -> dict | None:
    _KIND, _SID = "refusal", f"{cfg['type']}_{idx}"
    try:
        raw = _gen(
            cfg["prompt"], _REFUSAL_SYSTEM, 600,
            f"eval_refusal_{cfg['type']}_{idx}", 0.85, seed + idx,
        )
    except Exception as exc:
        _log_fail(_KIND, _SID, "api_error", f"{type(exc).__name__}: {exc}")
        return None

    qa = _parse_qa(raw)
    if not qa:
        _log_fail(_KIND, _SID, "parse_failed", raw)
        return None

    return {
        "id": _sid("rf", cfg["type"], str(idx)),
        "type": "refusal",
        "question": qa["question"],
        "gold_clauses": [],
        "gold_values": [],
        "gold_verified": True,          # 拒答题无金标数值，无需回锚
        "gold_answer": qa.get("gold_answer", ""),
        "should_refuse": True,
        "refusal_type": cfg["type"],
        "source": "AI生成/拒答",
        "difficulty": "medium",
    }


# ═══════════════════════════════════════════════════════════════════════════
# 主逻辑：策略选题
# ═══════════════════════════════════════════════════════════════════════════

_QUOTA = {
    "single_clause": 120,
    "cross_clause":  100,
    "calculation":    80,
    "clause_verify":  60,
    "refusal":        40,
}

_STD_WEIGHTS = {
    "GB50010-2010": 0.23,
    "GB50011-2010": 0.23,
    "GB50007-2011": 0.18,
    "GB50009-2012": 0.07,
    "JGJ3-2010":    0.29,
}


def _select_clauses(clauses: list[dict], n: int, rng: random.Random, prefer_tables: bool = False) -> list[dict]:
    """按标准权重采样，prefer_tables 时优先含表格条文。"""
    by_std: dict[str, list[dict]] = {}
    for c in clauses:
        by_std.setdefault(c["standard_code"], []).append(c)

    result = []
    for std, weight in _STD_WEIGHTS.items():
        pool = by_std.get(std, [])
        if not pool:
            continue
        if prefer_tables:
            # 原实现是 sorted(...) 后交给 rng.choices —— 但 choices 不带权重、
            # 对顺序无感，排序完全没生效。改为直接把候选池收窄到含表格的条文；
            # 数量不够时退回全池，避免某本规范因表格少而抽不满配额。
            preferred = [c for c in pool if c.get("tables")]
            pool = preferred or pool
        k = min(max(1, round(n * weight)), len(pool))
        # **无放回**抽样：原用 rng.choices（有放回），同一条文可能被抽中多次，
        # 进而对同一条文出两道高度相似甚至完全相同的题，
        # 且 _sid 以 clause_id 为哈希输入，会产生重复 id。
        result.extend(rng.sample(pool, k))

    # 补齐到 n（各标准取整误差所致），同样不放回、不与已选重复
    if len(result) < n:
        chosen = {c["clause_id"] for c in result}
        rest = [c for c in clauses if c["clause_id"] not in chosen]
        rng.shuffle(rest)
        result.extend(rest[: n - len(result)])
    return result[:n]


def _build_ref_pairs(clauses: list[dict], cmap: dict[str, dict]) -> list[tuple[dict, dict]]:
    seen: set[frozenset] = set()
    pairs = []
    for c in clauses:
        for ref_id in c.get("refs", []):
            if ref_id not in cmap:
                continue
            key = frozenset([c["clause_id"], ref_id])
            if key in seen:
                continue
            seen.add(key)
            pairs.append((c, cmap[ref_id]))
    return pairs


def build_evalset(smoke: bool = False, workers: int = 1, seed: int = 42) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _OUT_DIR / "evalset_v1.jsonl"

    clauses = _load_clauses(_CLAUSES)
    cmap = _clause_map(clauses)
    rng = random.Random(seed)

    quota = {k: 5 if smoke else v for k, v in _QUOTA.items()}

    # ── 含数值的条文池 ───────────────────────────────────────────────────
    # single_clause 与 calculation 都要考数值，必须从含表格或含数值的条文里选。
    # 早先 single_clause 在全库抽样，抽到「2.1.1 永久荷载的定义」这类术语条款——
    # 条文里根本没有数值，模型只能凭空编，回锚后 gold_values 全空，
    # 该题在阶段 5.4（数值精确匹配）完全无法判分。
    numeric_pool = [c for c in clauses if c.get("tables") or re.search(r"\d+\.\d+", c["text"])]
    calc_pool = numeric_pool

    # ── 条款验证：40 真 + 20 诱导（smoke 各一半）──────────────────────────
    verify_true_n = round(quota["clause_verify"] * 2 / 3)
    verify_trap_n = quota["clause_verify"] - verify_true_n

    # ── 构建任务列表 ──────────────────────────────────────────────────────
    tasks: list[tuple[str, tuple]] = []

    # single_clause
    for c in _select_clauses(numeric_pool, quota["single_clause"], rng, prefer_tables=True):
        tasks.append(("single", (c, seed)))

    # cross_clause
    pairs = _build_ref_pairs(clauses, cmap)
    rng.shuffle(pairs)
    for a, b in pairs[:quota["cross_clause"]]:
        tasks.append(("cross", (a, b, seed)))

    # calculation
    for c in _select_clauses(calc_pool, quota["calculation"], rng, prefer_tables=True):
        tasks.append(("calc", (c, seed)))

    # clause_verify
    verify_clauses = _select_clauses(clauses, verify_true_n + verify_trap_n, rng)
    for i, c in enumerate(verify_clauses[:verify_true_n]):
        tasks.append(("verify_true", (c, False, seed + i)))
    for i, c in enumerate(verify_clauses[verify_true_n:]):
        tasks.append(("verify_trap", (c, True, seed + i + 100)))

    # refusal
    for cfg in _REFUSAL_CONFIGS:
        n = max(1, round(quota["refusal"] * cfg["quota_ratio"]))
        for i in range(n):
            tasks.append(("refusal", (cfg, i, seed)))

    print(f"[gen_evalset] 任务数：{len(tasks)}")

    write_lock = threading.Lock()
    results: list[dict] = []
    counts: dict[str, int] = {k: 0 for k in _QUOTA}

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(tasks), desc="evalset gen")
    except ImportError:
        pbar = None

    def _dispatch(task):
        kind, args = task
        if kind == "single":
            return gen_single_clause(*args)
        elif kind == "cross":
            return gen_cross_clause(*args)
        elif kind == "calc":
            return gen_calculation(*args)
        elif kind in ("verify_true", "verify_trap"):
            return gen_clause_verify(*args)
        elif kind == "refusal":
            return gen_refusal(*args)
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_dispatch, t) for t in tasks]
        for fut in as_completed(futures):
            item = fut.result()
            if pbar:
                pbar.update(1)
            if item is None:
                continue
            with write_lock:
                results.append(item)
                counts[item["type"]] = counts.get(item["type"], 0) + 1

    if pbar:
        pbar.close()

    # 写出
    results = _dedup(results)

    with open(out_file, "w", encoding="utf-8") as f:
        for item in results:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    total = len(results)
    print(f"\n[gen_evalset] 生成 {total} 题")
    for k, v in counts.items():
        print(f"  {k}: {v}/{_QUOTA[k]}")

    # 金标覆盖率：阶段 5.4 是数值精确匹配判分，gold_values 为空的题在那一项
    # 完全无法评分。覆盖率低不必然是 bug（拒答题本就无金标、跨条文题的答案
    # 常为推导值），但必须可见——否则会在出分时才发现半数题目不可判。
    scorable = [r for r in results if r["type"] != "refusal"]
    with_gold = [r for r in scorable if r["gold_values"]]
    if scorable:
        print(f"\n[gen_evalset] 金标覆盖（非拒答题）：{len(with_gold)}/{len(scorable)}"
              f" = {len(with_gold)/len(scorable):.0%}")
        by_type: dict[str, list[int]] = {}
        for r in scorable:
            by_type.setdefault(r["type"], []).append(1 if r["gold_values"] else 0)
        for k, v in sorted(by_type.items()):
            print(f"    {k:<16} {sum(v)}/{len(v)}")
    print_cost_summary()

    manifest = {
        "version": "v1",
        "total": total,
        "type_counts": counts,
        "quota": _QUOTA,
        # 出题模型必须如实记录：评测集是全实验的尺子，用哪个模型出的题
        # 直接关系到与训练数据的相关性（见 _gen 上方说明）。
        "provider": PROVIDER,
        "question_model": (
            __import__("src.utils.claude", fromlist=["x"]).DEFAULT_MODEL
            if PROVIDER == "claude" else _MODEL
        ),
        "effort": (
            __import__("src.utils.claude", fromlist=["x"]).DEFAULT_EFFORT
            if PROVIDER == "claude" else None
        ),
        # Anthropic API 无 seed 参数——出题的可复现性靠 prompt + 条文固定，
        # 而非采样种子。铁律 7 在此只能部分满足，须写入 EXPERIMENT.md 局限。
        "seed_supported": PROVIDER != "claude",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed,
        "smoke": smoke,
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[gen_evalset] → {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    build_evalset(smoke=args.smoke, workers=args.workers, seed=args.seed)
