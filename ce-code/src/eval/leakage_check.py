"""
阶段 3.3：泄漏检查（铁律 3 红线）

将评测集每道题与四组训练数据（A/B/C/D）的**问题**做向量相似度检查，
cosine > 0.9 判为泄漏，输出替换清单。

铁律：发现泄漏必须**替换重出**，不能删除——删除会破坏题型配额。

三个设计要点：

1. **embedding 走远程服务，与过滤器③同源**。原实现硬编码本地
   `SentenceTransformer("BAAI/bge-small-zh-v1.5")`，而服务器上跑的是 bge-large
   （:8097）。两处用不同模型算相似度，阈值就没有可比性；且本地路径实测会
   卡在 hf-mirror 超时重试五次。复用 dedup 的 `_RemoteEmbedder` 而非再写一份
   （判官曾因三处副本、换代只改一份而静默走回旧逻辑，同类错误不再重演）。

2. **必须先验数据是否与当前条文库同源**。评测集与训练数据都派生自条文库，
   若某组是旧库产物（本项目发生过：条文库两次大修后 C/D 组仍是旧数据），
   拿它做泄漏检查等于用过期数据给新数据背书，得到的"无泄漏"是假的。
   判据用**内容指纹**而非 clause_id 集合——旧库的 bug 是"正文被条文说明覆盖"，
   条款号一个没变，按 id 比对会误报同源。manifest 缺指纹时如实报"无法验证"，
   红线检查宁可说不知道也不给虚假绿灯。

3. **分组报告 + 相似度分布**。只报全局最大值无法区分"A 组模板句式撞车"
   与"C/D 组真泄漏"——前者是模板天然相似（A 组问题形如"请说明X第Y条的规定"），
   后者才是 LLM 生成时撞上了考题。两者处理方式不同，必须分开看。

运行：
  python -m src.eval.leakage_check                                  # 用远程 embedding
  python -m src.eval.leakage_check --embed-url http://localhost:8097
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

_EVAL_FILE = _ROOT / "data/eval/evalset_v1.jsonl"
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_TRAIN_FILES = {
    "a": _ROOT / "data/processed/group_a/train.jsonl",
    "b": _ROOT / "data/processed/group_b/train.jsonl",
    "c": _ROOT / "data/processed/group_c/train.jsonl",
    "d": _ROOT / "data/processed/group_d/train.jsonl",
}
_REPORT_PATH = _ROOT / "data/eval/leakage_report.md"
_THRESHOLD = 0.9


def _check_freshness() -> list[str]:
    """检查各组训练数据是否派生自当前条文库（比对内容指纹）。

    **不能只比对 clause_id 集合**：条文库的历史 bug 是「正文被条文说明覆盖」，
    条款号完全没变、变的是内容。旧数据引用的 id 在新库里照样存在，集合差为空——
    按 id 比对会误报「同源」，而那恰是本项目真实发生过的情况。故改比对
    manifest 里记录的 clauses_fingerprint。

    manifest 缺该字段时**如实报「无法验证」而非默认通过**：这类红线检查
    宁可说不知道，也不能给出虚假的绿灯。

    Args:
        无

    Returns:
        告警信息列表（为空表示各组均确认与当前条文库同源）
    """
    from src.utils.fingerprint import clauses_fingerprint
    current = clauses_fingerprint()
    if not current:
        return [f"条文库不存在：{_CLAUSES}"]

    warnings: list[str] = []
    for group, path in _TRAIN_FILES.items():
        if not path.exists():
            warnings.append(f"{group} 组数据缺失：{path.name}")
            continue
        mf = path.parent / "manifest.json"
        if not mf.exists():
            warnings.append(f"{group} 组无 manifest，无法验证是否与当前条文库同源")
            continue
        fp = json.loads(mf.read_text(encoding="utf-8")).get("clauses_fingerprint")
        if not fp:
            warnings.append(
                f"{group} 组 manifest 未记录 clauses_fingerprint（构建于该字段引入之前），"
                f"无法验证——重建该组后再查"
            )
        elif fp != current:
            warnings.append(
                f"{group} 组指纹 {fp} ≠ 当前条文库 {current}——该组是旧库产物，需重建后再查"
            )
    return warnings


def run(embed_url: str | None = None) -> None:
    """对评测集做泄漏检查并写报告。

    Args:
        embed_url: 远程 embedding 服务 base_url；为空则回落到本地模型

    Returns:
        None（报告写入 data/eval/leakage_report.md）
    """
    import numpy as np
    # 复用过滤器③的 embedder，保证两处用同一个模型算相似度
    from src.filter.dedup import _get_embed_model

    print("[leakage_check] ① 数据新鲜度检查...")
    warnings = _check_freshness()
    for w in warnings:
        print(f"  ⚠️  {w}")
    if not warnings:
        print("  ✅ 各组均派生自当前条文库")

    eval_items = [json.loads(l) for l in _EVAL_FILE.open(encoding="utf-8") if l.strip()]
    print(f"\n[leakage_check] 评测集：{len(eval_items)} 题")

    model = _get_embed_model(embed_url)
    print(f"[leakage_check] embedding: {'远程 ' + embed_url if embed_url else '本地 bge-small'}")
    eval_embs = np.asarray(model.encode([q["question"] for q in eval_items],
                                        normalize_embeddings=True))

    # 逐组算相似度：分组保留最大值，才能区分"A 组模板撞车"与"C/D 真泄漏"
    per_group_max: dict[str, "np.ndarray"] = {}
    per_group_arg: dict[str, "np.ndarray"] = {}
    per_group_qs: dict[str, list[str]] = {}
    counts: dict[str, int] = {}

    for group, path in _TRAIN_FILES.items():
        if not path.exists():
            continue
        qs = [json.loads(l)["conversations"][0]["value"]
              for l in path.open(encoding="utf-8") if l.strip()]
        counts[group] = len(qs)
        per_group_qs[group] = qs
        print(f"  {group} 组 {len(qs)} 条，编码中...")

        best = np.full(len(eval_items), -1.0, dtype=np.float32)
        best_j = np.zeros(len(eval_items), dtype=np.int64)
        step = 2048                       # 分批：避免一次性把 3 万条向量与矩阵都驻留内存
        for i in range(0, len(qs), step):
            embs = np.asarray(model.encode(qs[i:i + step], normalize_embeddings=True))
            sims = eval_embs @ embs.T     # (n_eval, batch)
            local_arg = sims.argmax(axis=1)
            local_max = sims.max(axis=1)
            upd = local_max > best
            best_j[upd] = local_arg[upd] + i
            best[upd] = local_max[upd]
        per_group_max[group] = best
        per_group_arg[group] = best_j

    if not counts:
        print("没有任何训练数据可比对")
        return

    # 汇总
    leaks: list[dict] = []
    all_max = np.max(np.vstack([per_group_max[g] for g in counts]), axis=0)
    for i, item in enumerate(eval_items):
        if all_max[i] <= _THRESHOLD:
            continue
        worst = max(counts, key=lambda g: per_group_max[g][i])
        leaks.append({
            "eval_id": item["id"],
            "eval_type": item["type"],
            "eval_question": item["question"][:100],
            "max_cosine": round(float(all_max[i]), 4),
            "matched_group": worst,
            "matched_question": per_group_qs[worst][int(per_group_arg[worst][i])][:100],
            "per_group": {g: round(float(per_group_max[g][i]), 4) for g in counts},
        })

    _write_report(leaks, eval_items, counts, all_max, per_group_max, warnings)

    print(f"\n[leakage_check] 泄漏 {len(leaks)}/{len(eval_items)} 题（阈值 > {_THRESHOLD}）")
    print("[leakage_check] 各组最高相似度分布：")
    for g in counts:
        arr = per_group_max[g]
        print(f"  {g} 组  中位 {float(np.median(arr)):.3f}  "
              f"p95 {float(np.percentile(arr, 95)):.3f}  "
              f"最大 {float(arr.max()):.3f}  "
              f"超阈值 {int((arr > _THRESHOLD).sum())} 题")
    print(f"[leakage_check] 报告 → {_REPORT_PATH}")
    if warnings:
        print("\n⚠️  上面的新鲜度告警未解决前，本次结论不可信")


def _write_report(
    leaks: list[dict],
    eval_items: list[dict],
    counts: dict[str, int],
    all_max,
    per_group_max: dict,
    warnings: list[str],
) -> None:
    """写 Markdown 报告。

    Args:
        leaks:         超阈值题目
        eval_items:    评测集全部题目
        counts:        各组样本数
        all_max:       每题跨组最大相似度
        per_group_max: 各组每题最大相似度
        warnings:      新鲜度告警

    Returns:
        None
    """
    import numpy as np

    n_eval = len(eval_items)
    lines = [
        "# 泄漏检查报告",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC",
        f"评测集：{n_eval} 题　阈值：cosine > {_THRESHOLD}",
        "",
        "训练数据：" + "　".join(f"{g} 组 {n}" for g, n in counts.items()),
        "",
    ]

    if warnings:
        lines += ["## ⚠️ 数据新鲜度告警", "",
                  "以下问题未解决前，本报告结论不可信（拿旧数据给新数据背书）：", ""]
        lines += [f"- {w}" for w in warnings] + [""]

    lines += [f"## 结论", "", f"**泄漏题数：{len(leaks)} / {n_eval}**", ""]
    lines += ["✅ 无泄漏，评测集可用。", ""] if not leaks else []

    lines += ["## 相似度分布（各组每题的最高相似度）", "",
              "| 组 | 样本数 | 中位 | p95 | 最大 | >0.9 | 0.8~0.9 |",
              "|---|---|---|---|---|---|---|"]
    for g, n in counts.items():
        arr = per_group_max[g]
        lines.append(
            f"| {g} | {n} | {float(np.median(arr)):.3f} | {float(np.percentile(arr,95)):.3f} "
            f"| {float(arr.max()):.3f} | {int((arr > _THRESHOLD).sum())} "
            f"| {int(((arr > 0.8) & (arr <= _THRESHOLD)).sum())} |"
        )
    lines += ["",
              "> A 组问题由 5 个模板套出（形如「请说明X第Y条的规定」），句式天然相似，",
              "> 其高相似度未必是泄漏；C/D 组由 LLM 生成，撞车才是真泄漏信号。分开看。",
              ""]

    if leaks:
        lines += [
            "## 需替换的题目",
            "",
            "铁律 3：**替换重出，不得删除**（删除会破坏题型配额）。",
            "",
            "| eval_id | 类型 | 最大相似度 | 命中组 | a | b | c | d | 评测题 | 命中训练题 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for lk in leaks:
            pg = lk["per_group"]
            lines.append(
                f"| {lk['eval_id']} | {lk['eval_type']} | {lk['max_cosine']} | {lk['matched_group']} "
                + "".join(f"| {pg.get(g, '-')} " for g in ("a", "b", "c", "d"))
                + f"| {lk['eval_question']} | {lk['matched_question']} |"
            )
        lines += [
            "",
            "### 操作步骤",
            "1. 对上表每道题，重新生成替换题（相同类型、相同难度）",
            "2. 替换后重跑本脚本确认无泄漏",
            "3. 旧题存入 evalset_v1_replaced.jsonl 留档",
        ]

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed-url", default=None,
                    help="远程 embedding base_url（如 http://localhost:8097），"
                         "留空则用本地 bge-small")
    args = ap.parse_args()
    run(embed_url=args.embed_url)
