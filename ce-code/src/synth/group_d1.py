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
import itertools
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
_OUT_DIR = _ROOT / "data/processed/group_d1"
_FAILED_DIR = _ROOT / "data/interim/failed"

# 合成模型：调用处、样本元数据、manifest 三处同源，避免各写一份字面量后漂移
# （group_b 就因此把 Qwen3-32B-AWQ 记成了 qwen-max）。
_SYNTH_MODEL = "/models/Qwen3-32B-AWQ"

# 配对候选的最小条文长度：低于此值的是形如 'D.1 一般规定' 的纯章节标题壳（共 40 条），
# 无实质内容，配对只会浪费 LLM 调用。20 字这个门槛实测只排除标题壳、不误伤真条文
# （最短的真条文如「11.4.11 不应采用石板作为承重构件」为 21 字）。
_MIN_CLAUSE_CHARS = 20


sys.path.insert(0, str(_ROOT))
from src.utils.llm import call as llm_call, print_cost_summary
from src.utils import jsonx
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


def _build_pairs(
    clauses: dict[str, dict],
    pool_size: int = 10000,
    seed: int = 42,
) -> list[tuple[dict, dict]]:
    """构建候选条文对：refs 显式引用全用，不足部分从同节组合按种子抽样补足。

    为什么不只用 refs——refs 只有 390 对，按老跑 29% 的产出率最多出 117 条，
    离 3000 的目标差一个数量级；且**评测集的 93 道跨条文题金标对 100% 落在
    refs 池里**（gen_evalset 用的是同一个 refs 字段），只用 refs 等于让训练数据
    与考题同源，泄漏风险最高（铁律 3）。掺入同节抽样后 refs 占比降到 4%。

    为什么扩池不损失严谨性——"必须综合两条才能回答"这个性质**不由配对方式保证，
    而由质检闸保证**：两条分别单独喂判官，任一条单独能答就淘汰。refs 只是
    "找出可能相关的两条"的启发式，同节组合是同类启发式，闸不变则产出性质不变。

    候选条文先滤掉 <20 字的纯章节标题壳（形如 'D.1 一般规定'，共 40 条），
    它们无实质内容，配对只会浪费 LLM 调用。

    Args:
        clauses:   clause_id → 条文
        pool_size: 候选对总数上限
        seed:      同节抽样种子（铁律 7）

    Returns:
        (条文A, 条文B) 列表，refs 对在前
    """
    # 纯标题壳无实质内容，不参与配对
    usable = {cid: c for cid, c in clauses.items() if len(c["text"]) >= _MIN_CLAUSE_CHARS}

    seen: set[frozenset] = set()
    pairs: list[tuple[dict, dict]] = []

    # ① refs 显式引用：语义关联最强，全部保留
    for cid, clause in usable.items():
        for ref_id in clause.get("refs", []):
            if ref_id not in usable or ref_id == cid:
                continue
            key = frozenset([cid, ref_id])
            if key in seen:
                continue
            seen.add(key)
            pairs.append((clause, usable[ref_id]))
    n_refs = len(pairs)

    # ② 同节组合：按 (标准号, 一级章, 二级节) 分组，组内两两配对后按种子抽样
    if len(pairs) < pool_size:
        by_section: dict[tuple, list[str]] = {}
        for cid, c in usable.items():
            path = c.get("chapter_path") or []
            key = (c["standard_code"], path[0] if path else "", path[1] if len(path) > 1 else "")
            by_section.setdefault(key, []).append(cid)

        candidates: list[frozenset] = []
        for members in by_section.values():
            if len(members) < 2:
                continue
            for a, b in itertools.combinations(sorted(members), 2):
                key = frozenset([a, b])
                if key not in seen:
                    candidates.append(key)

        random.Random(seed).shuffle(candidates)
        for key in candidates[: pool_size - len(pairs)]:
            a, b = tuple(key)
            seen.add(key)
            pairs.append((usable[a], usable[b]))

    print(f"[group_d1] 候选对 {len(pairs)}（refs {n_refs} + 同节抽样 {len(pairs) - n_refs}，"
          f"seed={seed}，可用条文 {len(usable)}/{len(clauses)}）")
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

    # 解析 JSON——走 jsonx 而非裸 json.loads：条文含大量 LaTeX，模型照抄
    # `\\leqslant`、`\\gamma` 等非法 JSON 转义，B 组曾因此丢掉 7.4% 的条文。
    qa = jsonx.extract(raw, kind="object")
    if not isinstance(qa, dict):
        _log_reject(clause_a, clause_b, "json_parse_failed", raw)
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
    pool_size: int = 10000,
    limit: int = 0,
) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _FAILED_DIR.mkdir(parents=True, exist_ok=True)

    # 淘汰日志是追加写的，本次开跑前先归档上一轮：否则统计会把历史记录算进来。
    # B 组曾因此把三次跑的 336 条失败堆在一个文件里，整体百分比完全失真。
    rej_path = _FAILED_DIR / "group_d1_rejected.jsonl"
    if rej_path.exists() and rej_path.stat().st_size:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archived = rej_path.with_name(f"group_d1_rejected_{stamp}.jsonl")
        rej_path.rename(archived)
        print(f"[group_d1] 上一轮淘汰日志已归档 → {archived.name}")

    clauses = _load_clauses(_CLAUSES)
    pairs = _build_pairs(clauses, pool_size=pool_size, seed=seed)

    if smoke or limit:
        # 必须**跨池抽样**而非取前 N：pairs 是 refs 在前，取前 N 只会覆盖
        # refs 对，而同节抽样对占池子 96%——那样的小批测不出真实产出率，
        # 失去"先小批验证再全量"（§6.5）的意义。
        n = limit or 50
        pairs = [pairs[i] for i in
                 sorted(random.Random(seed).sample(range(len(pairs)), min(n, len(pairs))))]
        print(f"[group_d1] 小批模式：跨池抽 {len(pairs)} 对（seed={seed}）")

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

    # 按原因拆分淘汰量：只报总数分不清是"格式坏了"还是"校验判严了"，
    # 而两者的处理方式完全不同（前者修解析、后者调闸或认账）。
    rej_path = _FAILED_DIR / "group_d1_rejected.jsonl"
    if rej_path.exists():
        import collections
        reasons = collections.Counter(
            json.loads(line).get("reason", "?")
            for line in rej_path.open(encoding="utf-8") if line.strip()
        )
        print("[group_d1] 淘汰原因构成：")
        for r, n in reasons.most_common():
            print(f"           {r:<26}{n:>6} ({n/max(sum(reasons.values()),1):.0%})")
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
    parser.add_argument("--smoke", action="store_true", help="跨池抽 50 对试跑")
    parser.add_argument("--limit", type=int, default=0, help="跨池抽 N 对试跑（覆盖 --smoke）")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pool-size", type=int, default=10000,
                        help="候选条文对上限（refs 全用，其余从同节组合按 seed 抽样补足）")
    args = parser.parse_args()
    build_group_d1(smoke=args.smoke, workers=args.workers, seed=args.seed,
                   pool_size=args.pool_size, limit=args.limit)
