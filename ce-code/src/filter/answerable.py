"""
过滤器 1：可答性

判断：仅凭条文原文，能否完整回答该问题？
- 能答 → 保留
- 信息不足 → 淘汰

设计要点（2026-07-27 重写，两处都关系到数据质量与可复现性）：

1. **判官模型必须 ≠ 合成模型**（CLAUDE.md §7）。B 组由 Qwen3-32B-AWQ 生成，
   若判官仍是它，除了自评偏袒，更严重的是**错误相关**：生成时若误读条文，
   判分时带同一误解确认通过，这类错误对本闸结构性隐形。
   判官改用 Qwen3-8B —— 可答性是**判别式**任务（阅读理解二分类），
   不同于开放式质量评价，不要求判官强于生成器；换规模即可打破主要的错误相关。
   代价：8B 若理解力不足会误杀（false reject），故全量前须过 validate_judge.py。

2. **强制 YES/NO，不给模型「顺带作答」的机会**。旧版 prompt 写「若能回答请直接作答」，
   LLM 天然爱回答不爱认怂 → 判定偏松；且每条要生成整段答案（平均约 500 输出 token）
   而那答案根本没人用，6960 条跑了 24 小时。改为 max_tokens=10 的二选一后，
   输出 token 降两个数量级，判定也更稳。

运行（单独可跑，也被 group_c.py 调用）：
  python -m src.filter.answerable --input data/processed/group_b/train.jsonl --output data/interim/filtered_answerable.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

from src.utils.llm import call as llm_call, print_cost_summary

# ── 判官配置 ──────────────────────────────────────────────────────────────
# 判官必须与合成模型（/models/Qwen3-32B-AWQ）不同，理由见模块 docstring。
# 服务器上 endpoint 与模型名可能变动，故均可用环境变量覆盖。
# 实测：vllm-qwen3-8b 容器注册名为 qwen3-8b，暴露在本机 :8099（与 deer-flow 后端共用）。
# 合成模型 /models/Qwen3-32B-AWQ 在另一台 172.19.2.2:8001，故判官需独立 base_url。
JUDGE_MODEL = os.getenv("CE_JUDGE_MODEL", "qwen3-8b")
JUDGE_BASE_URL = os.getenv("CE_JUDGE_BASE_URL", "http://localhost:8099/v1")

_SYSTEM = (
    "你是建筑结构工程规范审核员。你的唯一任务是判断给定条文是否足以完整回答问题。"
    "只输出 YES 或 NO，不要解释，不要作答。"
)

_TEMPLATE = """【条文】
{clause_text}

【问题】
{question}

仅凭上述条文内容，能否完整、准确地回答该问题？
- 条文包含回答所需的全部信息 → 输出 YES
- 条文信息不足，需要条文之外的知识或数据 → 输出 NO

只输出 YES 或 NO 一个词。"""


_first_error_reported = False


def _report_first_error(exc: Exception, model: str, base_url: str | None) -> None:
    """首次调用异常时打印完整信息，之后静默。

    存在的理由：调用异常若被无声吞掉，配置问题（endpoint 不通、模型名写错、
    环境变量缺失）会伪装成「判官输出无法解析」，让人以为是数据或模型质量问题。
    实测踩过一次：缺 DASHSCOPE_API_KEY 导致 50/50 全部「解析失败」而 LLM 调用数为 0。

    Args:
        exc:      捕获到的异常
        model:    当时使用的模型名
        base_url: 当时使用的 endpoint

    Returns:
        None
    """
    global _first_error_reported
    if _first_error_reported:
        return
    _first_error_reported = True
    print(
        f"\n⚠️  判官调用失败（后续同类错误不再重复打印）\n"
        f"    模型     : {model}\n"
        f"    endpoint : {base_url}\n"
        f"    错误     : {type(exc).__name__}: {exc}\n"
        f"    排查     : 检查 endpoint 可达性与模型名，"
        f"curl {base_url}/models\n",
        file=sys.stderr,
    )


def judge_answerable(clause_text: str, question: str, sample_id: str = "") -> bool | None:
    """调用判官模型判定单条样本的可答性。

    Args:
        clause_text: 来源条文原文
        question:    待判定的问题
        sample_id:   样本 ID，仅用于失败留痕

    Returns:
        True=可答 / False=不可答 / None=调用失败或输出无法解析
    """
    prompt = _TEMPLATE.format(clause_text=clause_text, question=question)
    try:
        resp = llm_call(
            prompt,
            system=_SYSTEM,
            model=JUDGE_MODEL,
            max_tokens=10,          # 只需一个词；旧版 1024 是 24h 耗时的主因
            temperature=0.0,        # 铁律 7：过滤是数据构造的一环，必须可复现
            seed=42,
            sample_id=sample_id,
            base_url=JUDGE_BASE_URL,
            extra_body={"enable_thinking": False},
        )
    except Exception as exc:
        _report_first_error(exc, JUDGE_MODEL, JUDGE_BASE_URL)
        return None
    return _parse_verdict(resp)


def _parse_verdict(response: str) -> bool | None:
    """解析判官输出为布尔判定。

    Args:
        response: 模型原始输出

    Returns:
        True=YES / False=NO / None=两者都没出现（视为不可解析）
    """
    r = response.strip().upper()
    has_yes, has_no = "YES" in r, "NO" in r
    if has_yes == has_no:       # 都有或都无 → 无法判定
        return None
    return has_yes


def filter_answerable(
    input_path: Path,
    output_path: Path,
    rejected_path: Path,
    clauses_path: Path,
    model: str | None = None,
    seed: int = 42,
) -> tuple[int, int]:
    """对输入样本逐条做可答性过滤。

    Args:
        input_path:    待过滤样本 jsonl（B 组）
        output_path:   通过样本的输出路径
        rejected_path: 淘汰样本的输出路径（含 reject_reason，§6.6 失败留痕）
        clauses_path:  条文库 jsonl，用于按 source_clauses 取条文原文
        model:         覆盖判官模型；None 时用 JUDGE_MODEL
        seed:          保留参数，实际判定 temperature=0 已确定性

    Returns:
        (保留数, 淘汰数)
    """
    global JUDGE_MODEL
    if model:
        JUDGE_MODEL = model

    clause_map: dict[str, str] = {}
    with open(clauses_path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            clause_map[c["clause_id"]] = c["text"]

    with open(input_path, encoding="utf-8") as f:
        samples = [json.loads(line) for line in f]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)

    kept = rejected = 0
    try:
        from tqdm import tqdm
        iterator = tqdm(samples, desc=f"answerable[{JUDGE_MODEL}]")
    except ImportError:
        iterator = samples  # type: ignore[assignment]

    with open(output_path, "w", encoding="utf-8") as fout, \
         open(rejected_path, "w", encoding="utf-8") as frej:
        for sample in iterator:
            question = sample["conversations"][0]["value"]
            clause_ids = sample["meta"].get("source_clauses", [])
            clause_text = "\n\n".join(
                clause_map.get(cid, "") for cid in clause_ids if cid in clause_map
            ).strip()

            if not clause_text:
                sample["meta"]["reject_reason"] = "clause_not_found"
                frej.write(json.dumps(sample, ensure_ascii=False) + "\n")
                rejected += 1
                continue

            verdict = judge_answerable(clause_text, question, sample["sample_id"])

            if verdict is None:
                # 判官不可用或输出无法解析 → 保守保留，但打标记以便审计。
                # 不静默混入：淘汰率的分母含这些样本，报告需说明其占比。
                sample["meta"]["judge_failed"] = True
                fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
                kept += 1
            elif verdict:
                sample["meta"]["filters_passed"] = (
                    sample["meta"].get("filters_passed", []) + ["answerable"]
                )
                fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
                kept += 1
            else:
                sample["meta"]["reject_reason"] = "not_answerable"
                frej.write(json.dumps(sample, ensure_ascii=False) + "\n")
                rejected += 1

    total = kept + rejected
    print(f"[answerable] 判官={JUDGE_MODEL}")
    print(f"[answerable] 保留 {kept} / 淘汰 {rejected}（淘汰率 {rejected/total:.1%}）" if total else "[answerable] 无样本")
    print_cost_summary()
    return kept, rejected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rejected", default=None)
    parser.add_argument("--clauses", default=str(_ROOT / "data/interim/clauses.jsonl"))
    parser.add_argument("--model", default=None, help=f"判官模型，默认 {JUDGE_MODEL}")
    args = parser.parse_args()

    out = Path(args.output)
    rej = Path(args.rejected) if args.rejected else out.parent / "rejected_answerable.jsonl"
    filter_answerable(
        input_path=Path(args.input),
        output_path=out,
        rejected_path=rej,
        clauses_path=Path(args.clauses),
        model=args.model,
    )
