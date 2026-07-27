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
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ── 路径 ─────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_OUT_DIR = _ROOT / "data/processed/group_b"
_FAILED_DIR = _ROOT / "data/interim/failed"

# 单条条文进 prompt 的字数上限。vLLM max_model_len=32768 token，
# 中文约 1 字/token，留出 prompt 模板与输出的余量后取 2 万字。
_MAX_CLAUSE_CHARS = 20000
_PROMPT_FILE = _ROOT / "configs/prompts/synth_qa.txt"

sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, cost_tracker, print_cost_summary
from src.utils import jsonx
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
    """从 LLM 输出中提取 JSON 列表，兼容 ```json ... ``` 包裹与 LaTeX 转义。

    走 jsonx.extract：条文含大量 LaTeX，模型照抄 `\\leqslant`、`\\gamma` 等，
    其中多数不是合法 JSON 转义，直接 json.loads 会抛错。
    实测 2357 条里 174 条（7.4%）因此解析失败，超过本脚本自身 5% 的告警门线。

    Args:
        raw: 模型原始输出

    Returns:
        问答对列表；截取不到、解析失败或结果非列表时返回 None
    """
    data = jsonx.extract(raw, kind="array")
    return data if isinstance(data, list) else None


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
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = _FAILED_DIR / f"{date_tag}_parse_failed.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                # 存全量而非截断：raw[:1000] 会让任何超长输出在事后诊断中被误判为
                # 「被 max_tokens 截断」，从而把排查引向调大 max_tokens 这个错误方向。
                # 实测失败样本平均输出仅 590 token、上限 3000，根本不存在截断。
                {"ts": datetime.now(timezone.utc).isoformat(), "clause_id": clause_id,
                 "raw": raw, "raw_len": len(raw)},
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
    sample: int = 0,
) -> None:
    """按条文库生成 B 组问答样本。

    Args:
        smoke:  只跑前 50 条条文（快速验证流程是否通畅）
        resume: 断点续跑，跳过已生成的条文
        seed:   随机种子，同时用于 LLM 采样与 --sample 抽样（铁律 7）
        workers: 并发线程数
        sample: >0 时随机抽取该数量的条文（固定 seed）。与 --smoke 的区别是
                覆盖全部五本规范与各章节，适合做判官验证等需要代表性的场景——
                前 50 条集中在 GB50010 的总则与术语，样本形态偏窄。

    Returns:
        None（结果写入 data/processed/group_b/）
    """
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
    elif sample > 0:
        clauses = random.Random(seed).sample(clauses, min(sample, len(clauses)))
        print(f"[group_b] 随机抽样 {len(clauses)} 条条文（seed={seed}）")

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

    # 超长条款闸：附录里存在纯查表型的巨表（GB50009 附录E 全国城镇雪压风压表约
    # 11.5 万字、GB50007 附录K 附加应力系数表约 4 万字），整段塞进 prompt 会超出
    # vLLM 的 max_model_len（32768），请求直接失败。这类条款本身也不适合生成问答。
    # 按 CLAUDE.md §6.6 留痕，不静默丢弃。
    oversized = [c for c in pending if c["char_len"] > _MAX_CLAUSE_CHARS]
    if oversized:
        skip_path = _FAILED_DIR / "group_b_oversized.jsonl"
        with open(skip_path, "w", encoding="utf-8") as f:
            for c in oversized:
                f.write(json.dumps(
                    {"clause_id": c["clause_id"], "char_len": c["char_len"],
                     "tables": len(c["tables"]), "reason": "oversized_skip"},
                    ensure_ascii=False) + "\n")
        print(f"[group_b] 跳过超长条款 {len(oversized)} 条（>{_MAX_CLAUSE_CHARS} 字）→ {skip_path}")
        for c in oversized:
            print(f"           {c['clause_id']}  {c['char_len']} 字")
        pending = [c for c in pending if c["char_len"] <= _MAX_CLAUSE_CHARS]

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
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    parser.add_argument("--workers", type=int, default=1, help="并发线程数（本地 vLLM 实测 64 并发仍未饱和）")
    parser.add_argument("--sample", type=int, default=0,
                        help="随机抽取 N 条条文（固定 seed），覆盖面优于 --smoke 的前 N 条")
    args = parser.parse_args()
    build_group_b(smoke=args.smoke, resume=args.resume, seed=args.seed,
                  workers=args.workers, sample=args.sample)
