"""
阶段 2.3：Group B — LLM 反向生成问答（无过滤）

每条条文调用本地 vLLM 一次，生成 4 个问答对（施工员/设计师/监理/甲方视角）。
失败条文写入 data/interim/failed/，统计失败率后汇报。

运行：
  python -m src.synth.group_b --smoke             # 仅前 50 条，用于验证质量
  python -m src.synth.group_b --workers 8         # 全量，8 线程并发
  python -m src.synth.group_b --workers 8 --resume  # 断点续跑
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

# ── 路径 ─────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/processed/group_b"
_FAILED_DIR = _ROOT / "data/interim/failed"
_PROMPT_FILE = _ROOT / "configs/prompts/synth_qa.txt"

sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, cost_tracker, print_cost_summary
from src.synth.group_a import convert_text_tables  # HTML→Markdown 复用

# ── Prompt 加载（冻结文件）────────────────────────────────────────────────

def _load_prompt() -> tuple[str, str]:
    """返回 (system_prompt, user_template)"""
    text = _PROMPT_FILE.read_text(encoding="utf-8")
    sys_match = re.search(r"=== SYSTEM ===\n(.*?)\n=== USER TEMPLATE ===", text, re.DOTALL)
    usr_match = re.search(r"=== USER TEMPLATE ===\n(.*?)$", text, re.DOTALL)
    if not sys_match or not usr_match:
        raise ValueError(f"prompt 文件格式错误：{_PROMPT_FILE}")
    return sys_match.group(1).strip(), usr_match.group(1).strip()


_SYSTEM, _USER_TEMPLATE = _load_prompt()

# prompt 文件 hash（写入 manifest，保证可溯源）
_PROMPT_HASH = hashlib.md5(_PROMPT_FILE.read_bytes()).hexdigest()[:12]


# ── 单条处理 ─────────────────────────────────────────────────────────────

_PERSPECTIVES = ["施工员", "设计师", "监理", "甲方"]


def _build_prompt(clause: dict) -> str:
    clause_text = convert_text_tables(clause["text"])
    return (
        _USER_TEMPLATE
        .replace("{standard_name}", clause["standard_name"])
        .replace("{standard_code}", clause["standard_code"])
        .replace("{clause_no}", clause["clause_no"])
        .replace("{clause_text}", clause_text)
    )


def _parse_json_output(raw: str) -> list[dict] | None:
    """从 LLM 输出中提取 JSON 列表，兼容 ```json ... ``` 包裹。"""
    # 去掉 markdown 代码块
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    # 找最外层的 [ ... ]
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        if not isinstance(data, list):
            return None
        return data
    except json.JSONDecodeError:
        return None


def _sample_id(clause_id: str, perspective: str) -> str:
    h = hashlib.md5(f"{clause_id}_{perspective}".encode()).hexdigest()[:8]
    return f"b_{h}"


def _process_clause(clause: dict, seed: int = 42) -> list[dict] | None:
    """
    调用 LLM 生成 4 个样本。
    返回样本列表（成功），或 None（失败）。
    """
    prompt = _build_prompt(clause)
    try:
        raw = llm_call(
            prompt,
            system=_SYSTEM,
            model="/models/Qwen3-32B-AWQ",
            max_tokens=3000,
            temperature=0.8,
            seed=seed,
            sample_id=clause["clause_id"],
            extra_meta={"clause_id": clause["clause_id"]},
        )
    except Exception:
        return None

    qa_list = _parse_json_output(raw)
    if not qa_list:
        _log_parse_failure(clause["clause_id"], raw)
        return None

    samples = []
    for item in qa_list:
        perspective = item.get("perspective", "未知")
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if not question or not answer:
            continue
        samples.append(
            {
                "sample_id": _sample_id(clause["clause_id"], perspective),
                "group": "b",
                "conversations": [
                    {"from": "human", "value": question},
                    {"from": "gpt", "value": answer},
                ],
                "meta": {
                    "source_clauses": [clause["clause_id"]],
                    "sample_type": "single_clause",
                    "synth_model": "qwen-max",
                    "perspective": perspective,
                    "quality_score": None,
                    "filters_passed": [],
                    "prompt_hash": _PROMPT_HASH,
                },
            }
        )
    return samples if samples else None


def _log_parse_failure(clause_id: str, raw: str) -> None:
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.utcnow().strftime("%Y%m%d")
    path = _FAILED_DIR / f"{date_tag}_parse_failed.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"ts": datetime.utcnow().isoformat(), "clause_id": clause_id, "raw": raw[:1000]},
                ensure_ascii=False,
            )
            + "\n"
        )


# ── 主逻辑 ───────────────────────────────────────────────────────────────

def build_group_b(
    smoke: bool = False,
    resume: bool = False,
    seed: int = 42,
    workers: int = 1,
) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _OUT_DIR / "train.jsonl"
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)

    # 加载条文
    clauses: list[dict] = []
    with open(_CLAUSES, encoding="utf-8") as f:
        for line in f:
            clauses.append(json.loads(line))
    if smoke:
        clauses = clauses[:50]

    # resume：跳过已有的 source_clauses
    already_done: set[str] = set()
    if resume and out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            for line in f:
                try:
                    s = json.loads(line)
                    for cid in s.get("meta", {}).get("source_clauses", []):
                        already_done.add(cid)
                except Exception:
                    pass
        print(f"[group_b] resume 模式：已跳过 {len(already_done)} 条条文（已生成）")

    pending = [c for c in clauses if c["clause_id"] not in already_done]

    total_ok = 0
    total_fail = 0
    write_lock = threading.Lock()
    write_mode = "a" if (resume and out_file.exists()) else "w"

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(pending), desc="group_b synth")
    except ImportError:
        pbar = None

    def _handle(clause: dict) -> tuple[list[dict] | None, str]:
        return _process_clause(clause, seed=seed), clause["clause_id"]

    with open(out_file, write_mode, encoding="utf-8") as fout:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_handle, c): c for c in pending}
            for fut in as_completed(futures):
                samples, cid = fut.result()
                if pbar:
                    pbar.update(1)
                if samples is None:
                    with write_lock:
                        total_fail += 1
                    continue
                with write_lock:
                    for s in samples:
                        fout.write(json.dumps(s, ensure_ascii=False) + "\n")
                    total_ok += len(samples)

    if pbar:
        pbar.close()

    # 统计
    total_clauses = len(clauses) - len(already_done)
    fail_rate = total_fail / total_clauses if total_clauses else 0
    print(f"\n[group_b] 生成 {total_ok} 样本 | 失败 {total_fail}/{total_clauses} 条 ({fail_rate:.1%})")
    print_cost_summary()

    if fail_rate > 0.05:
        print(f"[group_b] ⚠️  失败率 {fail_rate:.1%} > 5%，建议检查 data/interim/failed/ 后再全量跑")

    # manifest
    manifest = {
        "group": "b",
        "version": "v1",
        "total_samples": total_ok,
        "total_clauses_attempted": total_clauses,
        "failed_clauses": total_fail,
        "fail_rate": round(fail_rate, 4),
        "synth_model": "qwen-max",
        "prompt_hash": _PROMPT_HASH,
        "seed": seed,
        "smoke": smoke,
        "built_at": datetime.utcnow().isoformat(timespec="seconds"),
        "cost": cost_tracker.summary(),
    }
    with open(_OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[group_b] manifest → {_OUT_DIR / 'manifest.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="仅处理前 50 条")
    parser.add_argument("--resume", action="store_true", help="跳过已生成条文（断点续跑）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=1, help="并发线程数（本地 vLLM 建议 4-8）")
    args = parser.parse_args()
    build_group_b(smoke=args.smoke, resume=args.resume, seed=args.seed, workers=args.workers)
