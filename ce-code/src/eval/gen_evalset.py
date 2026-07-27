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
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/eval"

sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, print_cost_summary
from src.synth.group_a import convert_text_tables

_MODEL = "/models/Qwen3-32B-AWQ"
# 思考模式已由 llm.call() 统一关闭（见 _build_extra_body），此处无需再传
_EXTRA: dict = {}

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
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if not obj.get("question") or not obj.get("answer"):
            return None
        return obj
    except json.JSONDecodeError:
        return None


def _extract_nums(text: str) -> list[str]:
    """从条文文本中提取数值字符串作为 gold_values 候选。"""
    nums = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    # 过滤掉条款号里的数字（如 8.2.1 → 8, 2, 1）
    return [n for n in nums if float(n) > 0.05][:8]


# ═══════════════════════════════════════════════════════════════════════════
# 各类型生成函数
# ═══════════════════════════════════════════════════════════════════════════

# ── single_clause ─────────────────────────────────────────────────────────

_SINGLE_SYSTEM = "你是建筑结构工程考试命题专家。基于提供的规范条文，出一道考查工程人员对该条文掌握情况的单选或简答题。"

_SINGLE_PROMPT = """以下是《{std_name}》（{std_code}）第{clause_no}条：

{text}

请出一道评测题，要求：
1. 考查该条文的核心数值、限值或要求，有明确唯一正确答案
2. 问题具体（给定工程场景和参数），不能含糊
3. 若条文含表格，优先考查表格中的具体数值
4. gold_answer 要包含条款号引用

输出格式（严格JSON）：
{{"question": "...", "gold_answer": "...", "gold_values": ["数值1", "数值2"]}}

gold_values 填写问题答案中的关键数值（无则填空列表）。"""


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
    try:
        raw = llm_call(
            prompt, system=_SINGLE_SYSTEM, model=_MODEL,
            max_tokens=800, temperature=0.5, seed=seed,
            sample_id=f"eval_single_{clause['clause_id']}",
            extra_body=_EXTRA,
        )
    except Exception:
        return None

    qa = _parse_qa(raw)
    if not qa:
        return None

    return {
        "id": _sid("sc", clause["clause_id"]),
        "type": "single_clause",
        "question": qa["question"],
        "gold_clauses": [clause["clause_id"]],
        "gold_values": qa.get("gold_values") or _extract_nums(clause["text"]),
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
2. 给定具体工程参数场景
3. gold_answer 引用两个条款号，给出完整结论

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
    try:
        raw = llm_call(
            prompt, system=_CROSS_SYSTEM, model=_MODEL,
            max_tokens=900, temperature=0.5, seed=seed,
            sample_id=f"eval_cross_{clause_a['clause_id']}_{clause_b['clause_id']}",
            extra_body=_EXTRA,
        )
    except Exception:
        return None

    qa = _parse_qa(raw)
    if not qa:
        return None

    return {
        "id": _sid("cc", clause_a["clause_id"], clause_b["clause_id"]),
        "type": "cross_clause",
        "question": qa["question"],
        "gold_clauses": [clause_a["clause_id"], clause_b["clause_id"]],
        "gold_values": qa.get("gold_values", []),
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
2. 要求查出或计算某一关键结果（限值/设计值/是否满足要求）
3. gold_values 必须填写正确答案中的关键数值
4. 答案步骤清晰

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
    try:
        raw = llm_call(
            prompt, system=_CALC_SYSTEM, model=_MODEL,
            max_tokens=1000, temperature=0.4, seed=seed,
            sample_id=f"eval_calc_{clause['clause_id']}",
            extra_body=_EXTRA,
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
        "gold_values": qa["gold_values"],
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
    try:
        raw = llm_call(
            prompt, system=_VERIFY_SYSTEM, model=_MODEL,
            max_tokens=700, temperature=0.5, seed=seed,
            sample_id=f"eval_verify_{clause['clause_id']}_{is_trap}",
            extra_body=_EXTRA,
        )
    except Exception:
        return None

    qa = _parse_qa(raw)
    if not qa:
        return None

    return {
        "id": _sid("cv", clause["clause_id"], str(is_trap)),
        "type": "clause_verify",
        "question": qa["question"],
        "gold_clauses": [clause["clause_id"]],
        "gold_values": qa.get("gold_values", []),
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
    try:
        raw = llm_call(
            cfg["prompt"], system=_REFUSAL_SYSTEM, model=_MODEL,
            max_tokens=600, temperature=0.85, seed=seed + idx,
            sample_id=f"eval_refusal_{cfg['type']}_{idx}",
            extra_body=_EXTRA,
        )
    except Exception:
        return None

    qa = _parse_qa(raw)
    if not qa:
        return None

    return {
        "id": _sid("rf", cfg["type"], str(idx)),
        "type": "refusal",
        "question": qa["question"],
        "gold_clauses": [],
        "gold_values": [],
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
            pool = sorted(pool, key=lambda c: (len(c.get("tables", [])) > 0, c.get("is_mandatory", False)), reverse=True)
        k = max(1, round(n * weight))
        result.extend(rng.choices(pool, k=k))

    # 补齐到 n（因为取整误差）
    while len(result) < n:
        result.append(rng.choice(clauses))
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

    # ── 计算题：优先含表格且有数值的条文 ─────────────────────────────────
    calc_pool = [c for c in clauses if c.get("tables") or re.search(r"\d+\.\d+", c["text"])]

    # ── 条款验证：40 真 + 20 诱导（smoke 各一半）──────────────────────────
    verify_true_n = round(quota["clause_verify"] * 2 / 3)
    verify_trap_n = quota["clause_verify"] - verify_true_n

    # ── 构建任务列表 ──────────────────────────────────────────────────────
    tasks: list[tuple[str, tuple]] = []

    # single_clause
    for c in _select_clauses(clauses, quota["single_clause"], rng, prefer_tables=True):
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
    with open(out_file, "w", encoding="utf-8") as f:
        for item in results:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    total = len(results)
    print(f"\n[gen_evalset] 生成 {total} 题")
    for k, v in counts.items():
        print(f"  {k}: {v}/{_QUOTA[k]}")
    print_cost_summary()

    manifest = {
        "version": "v1",
        "total": total,
        "type_counts": counts,
        "quota": _QUOTA,
        "synth_model": _MODEL,
        "built_at": datetime.utcnow().isoformat(timespec="seconds"),
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
