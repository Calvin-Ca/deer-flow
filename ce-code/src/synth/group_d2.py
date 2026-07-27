"""
阶段 2.9：D2 — 拒答样本

三类场景，配额约 5:3:2：
  A. 超出规范覆盖（需工程判断）    ~750 条
  B. 需现场数据                    ~450 条
  C. 规范冲突/地方标准差异          ~300 条

拒答回答要求：① 说明为什么无法回答 ② 指出相关规范章节 ③ 建议由谁来判断

目标规模：~1500 条
运行：
  python -m src.synth.group_d2 --smoke       # 各类型各 5 条
  python -m src.synth.group_d2 --workers 8   # 全量
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
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/processed/group_d2"
_FAILED_DIR = _ROOT / "data/interim/failed"

# 合成模型：调用处、样本元数据、manifest 三处同源，避免各写一份字面量后漂移
# （group_b 就因此把 Qwen3-32B-AWQ 记成了 qwen-max）。
_SYNTH_MODEL = "/models/Qwen3-32B-AWQ"


sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, print_cost_summary
from src.synth.group_a import convert_text_tables

# ── 拒答模板配置 ──────────────────────────────────────────────────────────

_SYSTEM = (
    "你是一位建筑结构工程领域专家，熟悉相关规范但也清楚自己的判断边界。"
    "你的任务是生成工程实践中确实无法仅凭规范条文回答、需要专业判断或现场数据的问题，"
    "以及符合专业规范的拒答回复。"
)

# 三类拒答 prompt
_TEMPLATES = {
    "A_beyond_scope": {
        "quota": 750,
        "prompt": """基于以下规范条文（仅供背景参考）：

【条文】{std}第{no}条
{text}

请生成一个工程场景问题，该问题的核心决策超出规范条文的直接回答范围，
需要工程师结合项目实际情况进行专业判断（如方案选择、材料取舍、构造优化等）。

然后给出一个专业的拒答回复，包含：① 说明规范只给出原则/限值，无法替代工程判断
② 指出该条款的适用范围 ③ 建议由有经验的结构工程师结合实际条件决定。

输出格式（严格JSON）：
{{"question": "...", "answer": "..."}}""",
    },
    "B_needs_field_data": {
        "quota": 450,
        "prompt": """基于以下规范条文（仅供背景参考）：

【条文】{std}第{no}条
{text}

请生成一个工程场景问题，该问题的回答需要现场实测数据、检测报告或具体工程参数，
规范条文无法给出直接结论（如"这根梁/柱还能不能用"、"裂缝/变形是否超标"类问题）。

然后给出专业的拒答回复，包含：① 说明需要哪些现场数据才能判断
② 引用相关条款说明验收标准 ③ 建议委托有资质的检测机构评估。

输出格式（严格JSON）：
{{"question": "...", "answer": "..."}}""",
    },
    "C_standard_conflict": {
        "quota": 300,
        "prompt": """基于以下规范条文（仅供背景参考）：

【条文】{std}第{no}条
{text}

请生成一个工程场景问题，涉及国标与地方标准（或新旧版本规范）之间的差异或冲突，
单凭一本规范条文无法给出明确结论。

然后给出专业的拒答回复，包含：① 说明存在规范差异的情况
② 指出应优先遵循的原则（通常是就严不就松或地方优先） ③ 建议由设计院或审图机构裁定。

输出格式（严格JSON）：
{{"question": "...", "answer": "..."}}""",
    },
}


# ── 处理单条 ─────────────────────────────────────────────────────────────

def _sample_id(clause_id: str, type_key: str, idx: int) -> str:
    h = hashlib.md5(f"{clause_id}_{type_key}_{idx}".encode()).hexdigest()[:8]
    return f"d2_{h}"


def _process_one(
    clause: dict,
    type_key: str,
    idx: int,
    seed: int = 42,
) -> dict | None:
    template_cfg = _TEMPLATES[type_key]
    text = convert_text_tables(clause["text"])
    prompt = template_cfg["prompt"].format(
        std=clause["standard_code"],
        no=clause["clause_no"],
        text=text,
    )
    try:
        raw = llm_call(
            prompt, system=_SYSTEM, model=_SYNTH_MODEL,
            max_tokens=1000, temperature=0.85, seed=seed + idx,
            sample_id=f"d2_{clause['clause_id']}_{type_key}",
        )
    except Exception:
        return None

    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        qa = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

    question = str(qa.get("question", "")).strip()
    answer = str(qa.get("answer", "")).strip()
    if not question or not answer:
        return None

    return {
        "sample_id": _sample_id(clause["clause_id"], type_key, idx),
        "group": "d2",
        "conversations": [
            {"from": "human", "value": question},
            {"from": "gpt", "value": answer},
        ],
        "meta": {
            "source_clauses": [clause["clause_id"]],
            "sample_type": "refusal",
            "refusal_type": type_key,
            "synth_model": _SYNTH_MODEL,
            "quality_score": None,
            "filters_passed": [],
        },
    }


# ── 主逻辑 ───────────────────────────────────────────────────────────────

def build_group_d2(
    smoke: bool = False,
    workers: int = 1,
    seed: int = 42,
) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)

    clauses: list[dict] = []
    with open(_CLAUSES, encoding="utf-8") as f:
        for line in f:
            clauses.append(json.loads(line))

    rng = random.Random(seed)

    # 构造任务列表：(clause, type_key, idx)
    tasks: list[tuple[dict, str, int]] = []
    for type_key, cfg in _TEMPLATES.items():
        quota = 5 if smoke else cfg["quota"]
        # 随机采样条文（有放回），保证数量
        sampled = rng.choices(clauses, k=quota)
        for i, clause in enumerate(sampled):
            tasks.append((clause, type_key, i))

    print(f"[group_d2] 任务数：{len(tasks)}（目标 {sum(c['quota'] for c in _TEMPLATES.values())} 条）")

    out_file = _OUT_DIR / "train.jsonl"
    write_lock = threading.Lock()
    total_ok = 0
    total_fail = 0
    type_counts: dict[str, int] = {k: 0 for k in _TEMPLATES}

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(tasks), desc="group_d2 synth")
    except ImportError:
        pbar = None

    def _handle(task):
        clause, type_key, idx = task
        return _process_one(clause, type_key, idx, seed=seed), type_key

    with open(out_file, "w", encoding="utf-8") as fout:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_handle, t) for t in tasks]
            for fut in as_completed(futures):
                result, type_key = fut.result()
                if pbar:
                    pbar.update(1)
                with write_lock:
                    if result is None:
                        total_fail += 1
                    else:
                        fout.write(json.dumps(result, ensure_ascii=False) + "\n")
                        total_ok += 1
                        type_counts[type_key] += 1

    if pbar:
        pbar.close()

    print(f"\n[group_d2] 生成 {total_ok} 条（失败 {total_fail}）")
    for k, v in type_counts.items():
        print(f"  {k}: {v}")
    print_cost_summary()

    manifest = {
        "group": "d2",
        "version": "v1",
        "total": total_ok,
        "type_counts": type_counts,
        "synth_model": _SYNTH_MODEL,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed,
        "smoke": smoke,
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[group_d2] → {out_file}（{total_ok} 条）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    build_group_d2(smoke=args.smoke, workers=args.workers, seed=args.seed)
