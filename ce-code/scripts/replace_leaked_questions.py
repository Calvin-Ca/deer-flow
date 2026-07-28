"""按铁律 3 替换泄漏题——同类型重出，不删除。

铁律 3 原文：「`cosine > 0.9` 的题目**直接替换，不是删除**」。删除会破坏
题型配额（single_clause 115 / cross_clause 93 / calculation 79 /
clause_verify 60 / refusal 39），配额一变，分题型准确率就不能与旧版比。

两个关键设计：

1. **换条文重出，不是拿同一条文再问一次**。撞车往往源于"这条条文就那么点内容，
   问法自然趋同"——同条文重出很可能再撞。故从未被评测集使用过的条文里选，
   既避开原碰撞源，又顺带扩大评测集的条文覆盖。

2. **新题必须立即过一遍泄漏检查**，通不过就换条文重试。否则可能换出一道新的
   泄漏题，而下一轮全量检查才发现——那是把问题推给未来的自己。

旧题存入 evalset_v1_replaced.jsonl 留档（DATA_SPEC §泄漏检查要求）。

用法：
    python scripts/replace_leaked_questions.py --embed-url http://localhost:8097 --dry-run
    python scripts/replace_leaked_questions.py --embed-url http://localhost:8097
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT))

_EVAL = _ROOT / "data/eval/evalset_v1.jsonl"
_REPLACED = _ROOT / "data/eval/evalset_v1_replaced.jsonl"
_REPORT = _ROOT / "data/eval/leakage_report.md"
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_TRAIN = {g: _ROOT / f"data/processed/group_{g}/train.jsonl" for g in "abcd"}
_THRESHOLD = 0.9
_MAX_ATTEMPTS = 5


def _leaked_ids() -> list[str]:
    """从泄漏报告里读出需替换的 eval_id。

    Args:
        无

    Returns:
        eval_id 列表
    """
    if not _REPORT.exists():
        print(f"缺泄漏报告：{_REPORT}，请先跑 src.eval.leakage_check")
        return []
    ids: list[str] = []
    in_table = False
    for line in _REPORT.read_text(encoding="utf-8").splitlines():
        if line.startswith("| eval_id |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|") or line.startswith("|---"):
                if ids:
                    break
                continue
            cell = line.split("|")[1].strip()
            if cell and cell != "eval_id":
                ids.append(cell)
    return ids


def _last_failure() -> str:
    """读出题失败日志的最后一条，供即时归因。

    gen_evalset 的各生成函数按 §6.6 把失败原因写进
    data/interim/failed/gen_evalset_failed.jsonl，但返回值只有 None。
    调用方若只报"生成失败"，就把静默失败原样传下去了——15 次全失败时
    分不清是模型偶发还是系统性问题（API key 未配、端点不通等）。

    Args:
        无

    Returns:
        形如 "reason: detail" 的单行摘要；无日志时说明情况
    """
    log = _ROOT / "data/interim/failed/gen_evalset_failed.jsonl"
    if not log.exists():
        return "（无失败日志，可能是异常在 gen_evalset 之外被吞掉）"
    try:
        last = None
        with log.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return "（失败日志为空）"
        rec = json.loads(last)
        return f"{rec.get('reason', '?')}: {str(rec.get('detail', ''))[:160]}"
    except Exception as exc:
        return f"（读失败日志出错：{exc}）"


def _preflight() -> bool:
    """开跑前用一次真实调用探测出题通道是否可用。

    没有这一步，API key 未配一类的问题会表现成"每道题都生成失败"，
    重试 5 次 × 3 题 = 15 次无谓调用后才暴露，且原因不明。
    宁可先花一次调用把话说清楚。

    Args:
        无

    Returns:
        通道可用返回 True
    """
    from src.eval import gen_evalset as G
    print(f"[replace] 探测出题通道：provider={G.PROVIDER} model={G._MODEL}")
    try:
        out = G._gen("回复 OK 两个字。", system="你是测试助手。",
                     max_tokens=16, sample_id="preflight")
    except Exception as exc:
        print(f"  ❌ 出题通道不可用：{type(exc).__name__}: {exc}")
        print("     若是鉴权错误，检查 DASHSCOPE_API_KEY 是否在环境里")
        return False
    if not out or not out.strip():
        print("  ❌ 出题通道返回空内容")
        return False
    print(f"  ✅ 通道可用，返回：{out.strip()[:40]}")
    return True


def _train_embeddings(model):
    """编码四组全部训练问题，供新题即时查重。

    Args:
        model: embedding 模型（须有 encode 方法）

    Returns:
        (向量矩阵, 问题文本列表)
    """
    import numpy as np
    qs: list[str] = []
    for g, path in _TRAIN.items():
        if path.exists():
            qs += [json.loads(l)["conversations"][0]["value"]
                   for l in path.open(encoding="utf-8") if l.strip()]
    print(f"[replace] 训练问题 {len(qs)} 条，编码中...")
    mats = []
    for i in range(0, len(qs), 2048):
        mats.append(np.asarray(model.encode(qs[i:i + 2048], normalize_embeddings=True)))
    return np.vstack(mats), qs


def _max_sim(question: str, model, train_embs, train_qs) -> tuple[float, str]:
    """算一道题与全部训练问题的最高相似度。

    Args:
        question:   题面
        model:      embedding 模型
        train_embs: 训练问题向量矩阵
        train_qs:   训练问题文本

    Returns:
        (最高相似度, 命中的训练问题)
    """
    import numpy as np
    v = np.asarray(model.encode([question], normalize_embeddings=True))
    sims = (v @ train_embs.T)[0]
    j = int(sims.argmax())
    return float(sims[j]), train_qs[j]


def main() -> int:
    """替换泄漏题并写回评测集。

    Args:
        无（命令行读 --embed-url / --dry-run / --seed）

    Returns:
        退出码：0 全部替换成功，1 有题未能替换
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed-url", default=None, help="embedding 服务 base_url")
    ap.add_argument("--dry-run", action="store_true", help="只生成不写盘")
    ap.add_argument("--seed", type=int, default=42, help="选条文的随机种子（铁律 7）")
    args = ap.parse_args()

    from src.filter.dedup import _get_embed_model
    from src.eval import gen_evalset as G

    ids = _leaked_ids()
    if not ids:
        print("泄漏报告里没有需替换的题目")
        return 0
    print(f"[replace] 待替换 {len(ids)} 题：{ids}\n")

    # 先探通道再干活：否则 API 不通会表现成"每道题都生成失败"，
    # 白跑 15 次调用 + 一轮 35091 条的 embedding 编码才暴露，且原因不明。
    if not _preflight():
        print("\n出题通道不可用，未做任何替换。")
        return 1

    items = [json.loads(l) for l in _EVAL.open(encoding="utf-8") if l.strip()]
    by_id = {it["id"]: it for it in items}
    clauses = G._load_clauses(_CLAUSES)
    cmap = G._clause_map(clauses)

    # 评测集已用过的条文——新题避开它们，既降低再撞概率，又扩大条文覆盖
    used = {c for it in items for c in (it.get("gold_clauses") or [])}
    fresh = [c for c in clauses if c["clause_id"] not in used and len(c["text"]) >= 50]
    print(f"[replace] 候选条文 {len(fresh)}（未被评测集使用且正文 ≥50 字）")

    model = _get_embed_model(args.embed_url)
    train_embs, train_qs = _train_embeddings(model)

    rng = random.Random(args.seed)
    replacements: dict[str, dict] = {}
    failed: list[str] = []

    for eid in ids:
        old = by_id.get(eid)
        if not old:
            print(f"  ⚠️ {eid} 不在评测集里，跳过")
            failed.append(eid)
            continue
        qtype = old["type"]
        print(f"\n── {eid}（{qtype}）──")
        print(f"   旧题：{old['question'][:60]}...")

        got = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if qtype == "cross_clause":
                pairs = G._build_ref_pairs(clauses, cmap)
                if not pairs:
                    break
                a, b = rng.choice(pairs)
                new = G.gen_cross_clause(a, b, seed=args.seed + attempt)
            elif qtype == "clause_verify":
                new = G.gen_clause_verify(rng.choice(fresh), is_trap=bool(attempt % 2),
                                          seed=args.seed + attempt)
            elif qtype == "calculation":
                new = G.gen_calculation(rng.choice(fresh), seed=args.seed + attempt)
            elif qtype == "single_clause":
                new = G.gen_single_clause(rng.choice(fresh), seed=args.seed + attempt)
            else:
                print(f"   ❌ 不支持的题型 {qtype}（refusal 题不依赖条文，另行处理）")
                break

            if not new:
                # 只报"生成失败"等于把静默失败原样传给使用者——15 次全失败时
                # 分不清是模型偶发、还是 API key 没配之类的系统性问题。
                # gen_evalset 已按 §6.6 把原因写进 failed 日志，这里把最新一条读出来。
                print(f"   第 {attempt} 次：生成失败 —— {_last_failure()}")
                continue
            sim, hit = _max_sim(new["question"], model, train_embs, train_qs)
            if sim > _THRESHOLD:
                print(f"   第 {attempt} 次：新题仍撞车 cos={sim:.4f}，换条文重试")
                continue
            # 保留原 id：阶段 5 按 id 索引，换 id 会让新旧结果无法对齐
            new["id"] = eid
            new["replaced_from"] = {
                "question": old["question"],
                "gold_clauses": old.get("gold_clauses"),
                "reason": "leakage_check cosine > 0.9",
                "replaced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            print(f"   ✅ 第 {attempt} 次通过 cos={sim:.4f}")
            print(f"   新题：{new['question'][:60]}...")
            got = new
            break

        if got:
            replacements[eid] = got
        else:
            print(f"   ❌ {_MAX_ATTEMPTS} 次尝试均未产出合格替换题")
            failed.append(eid)

    print(f"\n[replace] 成功 {len(replacements)} / {len(ids)}")
    if args.dry_run:
        print("[replace] dry-run，未写盘")
        return 1 if failed else 0

    if replacements:
        # 旧题留档（DATA_SPEC 要求），追加而非覆盖：历次替换都该保留
        with _REPLACED.open("a", encoding="utf-8") as f:
            for eid in replacements:
                f.write(json.dumps(by_id[eid], ensure_ascii=False) + "\n")
        shutil.copy2(_EVAL, _EVAL.with_suffix(".jsonl.bak"))
        with _EVAL.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(replacements.get(it["id"], it), ensure_ascii=False) + "\n")
        print(f"[replace] 评测集已更新（备份 {_EVAL.name}.bak）")
        print(f"[replace] 旧题留档 → {_REPLACED.name}")
        print("\n下一步：重跑 python -m src.eval.leakage_check --embed-url ... 确认无泄漏")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
