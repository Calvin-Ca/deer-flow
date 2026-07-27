"""
判官小批验证（CLAUDE.md §6.5：先用 50 条做小批验证，再全量跑）

目的：在把 6600 条样本交给新判官（Qwen3-8B）之前，先确认它判得准。
换判官的动机是打破「出题人兼判官」的错误相关（§7），但换成更小的模型有真实代价——
8B 若理解力不足会误杀本可回答的样本（false reject），让 C 组变小却未必更干净。
这一步就是用来量化该代价的。

做法：
  1. 从输入样本中抽 N 条（固定 seed，铁律 7）
  2. 同一批样本分别让**候选判官**与**对照判官**判定
  3. 输出两份产物：
     - review_*.md   人工复核用：条文 + 问题 + 两个判定，分歧样本排在最前
     - result_*.json 机器可读：逐条判定 + 一致率统计

你需要做的：打开 review md，对每条自己判一次「这段条文够不够回答这个问题」，
重点看分歧样本站在谁那边。若候选判官明显误杀，就别用它，回头上 Qwen-Max。

运行：
  python -m src.filter.validate_judge --input data/processed/group_b/train.jsonl --n 50
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

from src.filter.answerable import _TEMPLATE, _SYSTEM, _parse_verdict, _report_first_error
from src.utils.llm import call as llm_call, print_cost_summary

_OUT_DIR = _ROOT / "data/interim/judge_validation"

# 对照判官 = 合成模型本身。它正是要被替换掉的那个，用作基线以量化两者差异。
# 与候选判官不在同一台机器，故需各自的 base_url。
BASELINE_MODEL = os.getenv("CE_BASELINE_MODEL", "/models/Qwen3-32B-AWQ")
BASELINE_BASE_URL = os.getenv("CE_BASELINE_BASE_URL", "http://172.19.2.2:8001/v1")


def _judge(model: str, clause_text: str, question: str, sid: str,
           base_url: str | None = None) -> bool | None:
    """用指定模型对单条样本做可答性判定。

    Args:
        model:       判官模型名
        clause_text: 条文原文
        question:    待判问题
        sid:         样本 ID（失败留痕用）
        base_url:    该模型的 endpoint，None 走默认

    Returns:
        True=可答 / False=不可答 / None=失败或无法解析
    """
    try:
        resp = llm_call(
            _TEMPLATE.format(clause_text=clause_text, question=question),
            system=_SYSTEM, model=model, max_tokens=10, temperature=0.0,
            seed=42, sample_id=sid, base_url=base_url,
            extra_body={"enable_thinking": False},
        )
    except Exception as exc:
        _report_first_error(exc, model, base_url)
        return None
    return _parse_verdict(resp)


def _fmt(v: bool | None) -> str:
    """把判定值渲染成人类可读标记。

    Args:
        v: 判定值

    Returns:
        可读字符串
    """
    return {True: "YES 可答", False: "NO 不可答", None: "—— 失败/无法解析"}[v]


def validate(input_path: Path, clauses_path: Path, n: int,
             candidate: str, baseline: str, seed: int = 42) -> None:
    """抽样并用两个判官交叉判定，产出人工复核材料与一致率统计。

    Args:
        input_path:   样本 jsonl（B 组）
        clauses_path: 条文库 jsonl
        n:            抽样条数
        candidate:    候选判官模型名
        baseline:     对照判官模型名
        seed:         抽样随机种子（铁律 7：必须显式且写入产物）

    Returns:
        None（结果写入 data/interim/judge_validation/）
    """
    from src.filter.answerable import JUDGE_BASE_URL

    clause_map: dict[str, str] = {}
    with open(clauses_path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            clause_map[c["clause_id"]] = c["text"]

    with open(input_path, encoding="utf-8") as f:
        samples = [json.loads(line) for line in f]

    rng = random.Random(seed)
    picked = rng.sample(samples, min(n, len(samples)))
    print(f"[validate_judge] 从 {len(samples)} 条中抽样 {len(picked)} 条（seed={seed}）")
    print(f"[validate_judge] 候选判官：{candidate}  (base_url={JUDGE_BASE_URL})")
    print(f"[validate_judge] 对照判官：{baseline}  (base_url={BASELINE_BASE_URL})")

    try:
        from tqdm import tqdm
        iterator = tqdm(picked, desc="judging")
    except ImportError:
        iterator = picked  # type: ignore[assignment]

    rows = []
    for s in iterator:
        question = s["conversations"][0]["value"]
        cids = s["meta"].get("source_clauses", [])
        ctext = "\n\n".join(clause_map.get(c, "") for c in cids if c in clause_map).strip()
        if not ctext:
            continue
        rows.append({
            "sample_id": s["sample_id"],
            "source_clauses": cids,
            "question": question,
            "answer": s["conversations"][1]["value"],
            "clause_text": ctext,
            "candidate": _judge(candidate, ctext, question, s["sample_id"], JUDGE_BASE_URL),
            "baseline": _judge(baseline, ctext, question, s["sample_id"], BASELINE_BASE_URL),
        })

    if rows and all(r["candidate"] is None for r in rows):
        print("\n❌ 候选判官 50/50 全部失败——这是配置问题，不是判定结果。")
        print("   常见原因：endpoint 不通、模型名不符、环境变量缺失。")
        print(f"   自查：curl {JUDGE_BASE_URL}/models")
        sys.exit(1)

    agree = sum(1 for r in rows if r["candidate"] == r["baseline"])
    disagree = [r for r in rows if r["candidate"] != r["baseline"]]
    cand_yes = sum(1 for r in rows if r["candidate"] is True)
    base_yes = sum(1 for r in rows if r["baseline"] is True)
    cand_fail = sum(1 for r in rows if r["candidate"] is None)

    print(f"\n{'='*62}")
    print(f"  样本数        : {len(rows)}")
    print(f"  两judge一致   : {agree}/{len(rows)} = {agree/len(rows):.1%}" if rows else "  无样本")
    print(f"  候选判 YES    : {cand_yes}（淘汰率 {1-cand_yes/len(rows):.1%}）" if rows else "")
    print(f"  对照判 YES    : {base_yes}（淘汰率 {1-base_yes/len(rows):.1%}）" if rows else "")
    print(f"  候选解析失败  : {cand_fail}")
    print(f"{'='*62}")

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = _OUT_DIR / f"result_{tag}.json"
    json_path.write_text(json.dumps({
        "meta": {"input": str(input_path), "n": len(rows), "seed": seed,
                 "candidate": candidate, "baseline": baseline,
                 "built_at": datetime.now().isoformat(timespec="seconds")},
        "stats": {"agreement": agree / len(rows) if rows else 0,
                  "candidate_keep_rate": cand_yes / len(rows) if rows else 0,
                  "baseline_keep_rate": base_yes / len(rows) if rows else 0,
                  "candidate_parse_failed": cand_fail},
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 分歧样本排前面——人工时间应该花在这些上
    ordered = disagree + [r for r in rows if r["candidate"] == r["baseline"]]
    lines = [
        f"# 判官小批验证 {tag}",
        "",
        f"- 候选判官：`{candidate}`　对照判官：`{baseline}`",
        f"- 样本 {len(rows)} 条（seed={seed}），一致 {agree} 条（{agree/len(rows):.1%}）"
        if rows else "- 无样本",
        f"- 候选保留率 {cand_yes/len(rows):.1%}，对照保留率 {base_yes/len(rows):.1%}" if rows else "",
        "",
        "**怎么看**：逐条自己判「仅凭这段条文，能否完整回答这个问题」，",
        "再对照两个判官。重点看下方【分歧】部分——若候选判官把明显可答的判成 NO，",
        "说明它误杀严重，不宜用作判官。",
        "",
        f"## 分歧样本（{len(disagree)} 条，优先看）",
        "",
    ]
    for i, r in enumerate(ordered, 1):
        if i == len(disagree) + 1:
            lines += ["", f"## 一致样本（{agree} 条，抽查即可）", ""]
        mark = "⚠️ 分歧" if r["candidate"] != r["baseline"] else "一致"
        lines += [
            f"### {i}. {r['sample_id']}　{mark}",
            "",
            f"- 候选 `{candidate}`：**{_fmt(r['candidate'])}**",
            f"- 对照 `{baseline}`：**{_fmt(r['baseline'])}**",
            f"- 来源条文：{', '.join(r['source_clauses'])}",
            "",
            f"**问题**：{r['question']}",
            "",
            "<details><summary>条文原文</summary>",
            "",
            "```",
            r["clause_text"][:1500],
            "```",
            "",
            "</details>",
            "",
            "<details><summary>B 组生成的答案</summary>",
            "",
            r["answer"][:800],
            "",
            "</details>",
            "",
            "---",
            "",
        ]
    md_path = _OUT_DIR / f"review_{tag}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print_cost_summary()
    print(f"\n人工复核请打开：{md_path}")
    print(f"机器可读结果  ：{json_path}")


if __name__ == "__main__":
    from src.filter.answerable import JUDGE_MODEL

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(_ROOT / "data/processed/group_b/train.jsonl"))
    parser.add_argument("--clauses", default=str(_ROOT / "data/interim/clauses.jsonl"))
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--candidate", default=JUDGE_MODEL, help=f"候选判官，默认 {JUDGE_MODEL}")
    parser.add_argument("--baseline", default=BASELINE_MODEL)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    validate(Path(args.input), Path(args.clauses), args.n,
             args.candidate, args.baseline, args.seed)
