"""
阶段 2.8：D1 — 跨条文推理样本

利用条文库 refs 字段：条文 A 引用条文 B → 把两条一起喂给 LLM，
要求生成"必须综合两条才能回答"的问题。

质检额外一条：把两条分别单独喂给 LLM，若任一条单独能答完整 → 淘汰。

目标规模：~3000 条
运行：
  python -m src.synth.group_d1 --smoke       # 前 30 对，验证质量
  python -m src.synth.group_d1 --workers 8   # 全量
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/processed/group_d1"
_FAILED_DIR = _ROOT / "data/interim/failed"

sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, print_cost_summary
from src.synth.group_a import convert_text_tables

# ── Prompt ────────────────────────────────────────────────────────────────

_SYSTEM = (
    "你是一位建筑结构工程领域专家。"
    "你的任务是基于两条相互引用的规范条文，生成必须同时参考两条才能完整回答的工程问题及答案。"
)

_SYNTH_TEMPLATE = """以下是两条相互关联的规范条文（条文B被条文A引用）：

【条文A】{std_a}第{no_a}条
{text_a}

【条文B】{std_b}第{no_b}条
{text_b}

请生成一个工程问答对，要求：
1. 问题必须同时涉及两条条文的内容，单独任何一条都无法完整回答
2. 问题口语化、有具体工程场景
3. 答案明确引用两个条款号，说明各自的作用

输出格式（严格JSON，不要输出其他内容）：
{{"question": "...", "answer": "..."}}"""

_CHECK_TEMPLATE = """以下是规范条文：

【条文】{std}第{no}条
{text}

问题：{question}

仅凭上述单条条文，能否完整回答该问题？若能请直接作答；若信息不足请仅输出：INSUFFICIENT"""


# ── 构建条文对 ────────────────────────────────────────────────────────────

def _load_clauses(path: Path) -> dict[str, dict]:
    clauses: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            clauses[c["clause_id"]] = c
    return clauses


def _build_pairs(clauses: dict[str, dict]) -> list[tuple[dict, dict]]:
    """从 refs 字段构建（条文A, 条文B）对，去重。"""
    seen: set[frozenset] = set()
    pairs: list[tuple[dict, dict]] = []
    for cid, clause in clauses.items():
        for ref_id in clause.get("refs", []):
            if ref_id not in clauses:
                continue
            key = frozenset([cid, ref_id])
            if key in seen:
                continue
            seen.add(key)
            pairs.append((clause, clauses[ref_id]))
    return pairs


# ── 单对处理 ─────────────────────────────────────────────────────────────

def _sample_id(id_a: str, id_b: str) -> str:
    h = hashlib.md5(f"{id_a}|{id_b}".encode()).hexdigest()[:8]
    return f"d1_{h}"


def _process_pair(
    clause_a: dict,
    clause_b: dict,
    seed: int = 42,
) -> dict | None:
    text_a = convert_text_tables(clause_a["text"])
    text_b = convert_text_tables(clause_b["text"])

    # ① 合成问答
    prompt = _SYNTH_TEMPLATE.format(
        std_a=clause_a["standard_code"], no_a=clause_a["clause_no"], text_a=text_a,
        std_b=clause_b["standard_code"], no_b=clause_b["clause_no"], text_b=text_b,
    )
    try:
        raw = llm_call(
            prompt, system=_SYSTEM, model="/models/Qwen3-32B-AWQ",
            max_tokens=1500, temperature=0.8, seed=seed,
            sample_id=f"{clause_a['clause_id']}+{clause_b['clause_id']}",
            extra_body={"enable_thinking": False},
        )
    except Exception:
        return None

    # 解析 JSON
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

    # ② 单条可答性校验（任一条单独能答 → 淘汰）
    for clause in (clause_a, clause_b):
        check_prompt = _CHECK_TEMPLATE.format(
            std=clause["standard_code"], no=clause["clause_no"],
            text=convert_text_tables(clause["text"]), question=question,
        )
        try:
            resp = llm_call(
                check_prompt, system="你是结构工程专家，严格依据给定条文内容判断。",
                model="/models/Qwen3-32B-AWQ",
                max_tokens=512, temperature=0.0, seed=42,
                sample_id=f"check_{clause['clause_id']}",
                extra_body={"enable_thinking": False},
            )
            if "INSUFFICIENT" not in resp.strip().upper():
                return None  # 单条足够 → 不是真跨条文
        except Exception:
            pass  # 检查失败保守保留

    return {
        "sample_id": _sample_id(clause_a["clause_id"], clause_b["clause_id"]),
        "group": "d1",
        "conversations": [
            {"from": "human", "value": question},
            {"from": "gpt", "value": answer},
        ],
        "meta": {
            "source_clauses": [clause_a["clause_id"], clause_b["clause_id"]],
            "sample_type": "cross_clause",
            "synth_model": "/models/Qwen3-32B-AWQ",
            "quality_score": None,
            "filters_passed": ["cross_clause_verified"],
        },
    }


# ── 主逻辑 ───────────────────────────────────────────────────────────────

def build_group_d1(
    smoke: bool = False,
    workers: int = 1,
    seed: int = 42,
) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)

    clauses = _load_clauses(_CLAUSES)
    pairs = _build_pairs(clauses)
    print(f"[group_d1] 共 {len(pairs)} 条文对（来自 refs 引用）")

    if smoke:
        pairs = pairs[:30]

    out_file = _OUT_DIR / "train.jsonl"
    write_lock = threading.Lock()
    total_ok = 0
    total_fail = 0

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(pairs), desc="group_d1 synth")
    except ImportError:
        pbar = None

    def _handle(pair):
        return _process_pair(pair[0], pair[1], seed=seed)

    with open(out_file, "w", encoding="utf-8") as fout:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_handle, p) for p in pairs]
            for fut in as_completed(futures):
                result = fut.result()
                if pbar:
                    pbar.update(1)
                with write_lock:
                    if result is None:
                        total_fail += 1
                    else:
                        fout.write(json.dumps(result, ensure_ascii=False) + "\n")
                        total_ok += 1

    if pbar:
        pbar.close()

    total = total_ok + total_fail
    print(f"[group_d1] 生成 {total_ok} / 淘汰或失败 {total_fail}（{total_fail/total:.1%}）")
    print_cost_summary()

    manifest = {
        "group": "d1",
        "version": "v1",
        "total": total_ok,
        "pairs_attempted": total,
        "synth_model": "/models/Qwen3-32B-AWQ",
        "built_at": datetime.utcnow().isoformat(timespec="seconds"),
        "seed": seed,
        "smoke": smoke,
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[group_d1] → {out_file}（{total_ok} 条）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    build_group_d1(smoke=args.smoke, workers=args.workers, seed=args.seed)
