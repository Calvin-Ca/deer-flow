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
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/processed/group_d1"
_FAILED_DIR = _ROOT / "data/interim/failed"

# 合成模型：调用处、样本元数据、manifest 三处同源，避免各写一份字面量后漂移
# （group_b 就因此把 Qwen3-32B-AWQ 记成了 qwen-max）。
_SYNTH_MODEL = "/models/Qwen3-32B-AWQ"


sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, print_cost_summary
from src.filter.answerable import judge_answerable
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

# 单条可答性校验复用过滤器①的判官（8B + 强制 YES/NO），不再自带一份。
# 理由同 answerable.filter_answerable 的说明：judge 逻辑一旦有副本就会偏离，
# 实测 group_c 的并发副本在判官换代后仍在用 32B 与旧 prompt。


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

def _log_reject(clause_a: dict, clause_b: dict, reason: str, detail: str = "") -> None:
    """记录被淘汰的条文对及原因（CLAUDE.md §6.6：失败要留痕，不得静默丢弃）。

    历史教训：上一轮 D1 的 319 对里淘汰了 225 对（70.5%），但 JSON 解析失败与
    「单条即可答」两类都走同一个 `return None`，_FAILED_DIR 建了却从未写入，
    事后完全无法归因是格式问题还是校验判严了。

    Args:
        clause_a: 条文对中的 A
        clause_b: 条文对中的 B
        reason:   淘汰原因代码
        detail:   补充信息（模型原始输出片段等）

    Returns:
        None（追加写入 data/interim/failed/group_d1_rejected.jsonl）
    """
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)
    with open(_FAILED_DIR / "group_d1_rejected.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "pair": [clause_a["clause_id"], clause_b["clause_id"]],
            "reason": reason,
            "detail": detail,
        }, ensure_ascii=False) + "\n")


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
            prompt, system=_SYSTEM, model=_SYNTH_MODEL,
            max_tokens=1500, temperature=0.8, seed=seed,
            sample_id=f"{clause_a['clause_id']}+{clause_b['clause_id']}",
        )
    except Exception as exc:
        _log_reject(clause_a, clause_b, "synth_api_error", str(exc)[:200])
        return None

    # 解析 JSON
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        _log_reject(clause_a, clause_b, "no_json_in_output", raw[:200])
        return None
    try:
        qa = json.loads(m.group(0))
    except json.JSONDecodeError:
        _log_reject(clause_a, clause_b, "json_decode_error", raw[:200])
        return None

    question = str(qa.get("question", "")).strip()
    answer = str(qa.get("answer", "")).strip()
    if not question or not answer:
        _log_reject(clause_a, clause_b, "empty_question_or_answer", raw[:200])
        return None

    # ② 单条可答性校验（任一条单独能答 → 淘汰）
    # 复用过滤器①的判官（8B + 强制 YES/NO），语义相同的判断不应有两份实现。
    for clause in (clause_a, clause_b):
        verdict = judge_answerable(
            convert_text_tables(clause["text"]), question,
            sample_id=f"check_{clause['clause_id']}",
        )
        if verdict is True:          # 单条就能答 → 不是真跨条文
            _log_reject(clause_a, clause_b, "single_clause_sufficient",
                        f"{clause['clause_id']} 单独可答")
            return None
        # verdict is None（判官不可用/无法解析）→ 保守保留，与过滤器①一致

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
            "synth_model": _SYNTH_MODEL,
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
        "synth_model": _SYNTH_MODEL,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
