"""
过滤器 3：多样性去重

同一条文下的 4 个问题两两计算 embedding 余弦相似度，cosine > 0.85 判为重复，
每对只留一条。

取舍规则（2026-07-28 修正）——**组内先按固定种子洗牌再做贪心去重**：

  原实现写的是"保留 quality_score 最高的，分相同则保留靠前者"。但 quality_score
  从未实装：group_b 写死 None，前两道过滤也不打分，于是 `0.0 >= 0.0` 恒真，
  规则实际退化为"永远保留组内靠前者"。而组内顺序 = LLM 输出顺序 =
  施工员 → 设计师 → 监理 → 甲方，于是**甲方、监理被系统性淘汰**。

  去重本身没做错（重复确实被去掉了），错在"留谁"是系统性偏的：C/D 组的视角分布
  因此相对 B 组偏移，而消融要比的是"过滤 vs 不过滤"的质量差异，不是"甲方问题变少了"。

  改为按固定种子洗牌后再贪心，取舍无偏且可复现（铁律 7：seed 显式传入并记入
  manifest）。诚实边界：这不假装有质量信号，只保证取舍不带视角偏向——
  留下的那条不保证比被淘汰的更好。要真正按质量取舍需另加一轮 LLM 打分。

运行：
  python -m src.filter.dedup --input data/interim/filtered_clause.jsonl --output data/interim/filtered_dedup.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

# ── 相似度计算 ────────────────────────────────────────────────────────────

_EMBED_MODEL: object | None = None  # 懒加载


class _RemoteEmbedder:
    """通过 OpenAI-compatible /v1/embeddings 接口做 embedding（不依赖本地模型）。"""

    def __init__(self, base_url: str, model: str = "/model", batch_size: int = 64):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch_size = batch_size

    def encode(self, texts: list[str], normalize_embeddings: bool = True):
        import numpy as np, urllib.request, json as _json
        vecs = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i: i + self.batch_size]
            body = _json.dumps({"model": self.model, "input": batch}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/v1/embeddings",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = _json.loads(resp.read())
            for item in sorted(data["data"], key=lambda x: x["index"]):
                vecs.append(item["embedding"])
        arr = np.array(vecs, dtype=np.float32)
        if normalize_embeddings:
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            arr = arr / np.clip(norms, 1e-9, None)
        return arr


def _get_embed_model(embed_url: str | None = None):
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        if embed_url:
            _EMBED_MODEL = _RemoteEmbedder(embed_url)
        else:
            from sentence_transformers import SentenceTransformer
            _EMBED_MODEL = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    return _EMBED_MODEL


def _cosine(a, b) -> float:
    import numpy as np
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 1e-9 else 0.0


# ── 主逻辑 ────────────────────────────────────────────────────────────────

def filter_dedup(
    input_path: Path,
    output_path: Path,
    rejected_path: Path,
    threshold: float = 0.85,
    batch_size: int = 256,
    embed_url: str | None = None,
    seed: int = 42,
) -> tuple[int, int]:
    """按问题相似度对同一条文下的样本去重。

    Args:
        input_path:    待去重的 jsonl
        output_path:   保留样本输出路径
        rejected_path: 淘汰样本输出路径（留痕，§6.6）
        threshold:     余弦相似度超过此值视为重复
        batch_size:    embedding 批大小
        embed_url:     远程 embedding API base_url，为空则用本地 sentence-transformers
        seed:          组内洗牌种子（铁律 7：显式传入并由调用方记入 manifest）

    Returns:
        (保留条数, 淘汰条数)
    """
    samples: list[dict] = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            samples.append(json.loads(line))

    # 按 source_clauses 分组（同一条文的 4 个问题放一起）
    groups: dict[str, list[dict]] = {}
    for s in samples:
        key = "_".join(s["meta"].get("source_clauses", ["unknown"]))
        groups.setdefault(key, []).append(s)

    model = _get_embed_model(embed_url)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)

    kept_total = 0
    rejected_total = 0

    with open(output_path, "w", encoding="utf-8") as fout, \
         open(rejected_path, "w", encoding="utf-8") as frej:

        try:
            from tqdm import tqdm
            group_iter = tqdm(groups.items(), desc="dedup")
        except ImportError:
            group_iter = groups.items()  # type: ignore

        for clause_key, group in group_iter:
            if len(group) <= 1:
                # 单条直接保留
                for s in group:
                    s["meta"]["filters_passed"] = s["meta"].get("filters_passed", []) + ["diverse"]
                    fout.write(json.dumps(s, ensure_ascii=False) + "\n")
                    kept_total += 1
                continue

            questions = [s["conversations"][0]["value"] for s in group]
            embeddings = model.encode(questions, normalize_embeddings=True)

            # 遍历顺序按固定种子打乱，消除"永远淘汰靠后视角"的系统性偏向（见模块 docstring）。
            # 种子掺 clause_key：每组独立播种，结果不依赖组间迭代顺序，
            # 增删条文或改并发都不会扰动其余组的取舍。字符串种子经 sha512 转换，
            # 不走 hash()，故跨进程稳定（PYTHONHASHSEED 无影响）。
            order = list(range(len(group)))
            random.Random(f"{seed}:{clause_key}").shuffle(order)

            # 贪心去重：逐一检查，若与已保留的任一问题相似度 > threshold 则淘汰
            keep_flags = [True] * len(group)
            quality_scores = [s["meta"].get("quality_score") or 0.0 for s in group]

            for pos, i in enumerate(order):
                if not keep_flags[i]:
                    continue
                for j in order[pos + 1:]:
                    if not keep_flags[j]:
                        continue
                    sim = _cosine(embeddings[i], embeddings[j])
                    if sim > threshold:
                        # 质量分若已实装则淘汰低分者；未实装时两者皆 0.0，
                        # 淘汰洗牌后靠后的那条（即随机取舍）
                        if quality_scores[i] >= quality_scores[j]:
                            keep_flags[j] = False
                        else:
                            keep_flags[i] = False
                            break  # i 被淘汰，外层移到下一个

            for idx, (s, keep) in enumerate(zip(group, keep_flags)):
                if keep:
                    s["meta"]["filters_passed"] = s["meta"].get("filters_passed", []) + ["diverse"]
                    fout.write(json.dumps(s, ensure_ascii=False) + "\n")
                    kept_total += 1
                else:
                    s["meta"]["reject_reason"] = "too_similar"
                    frej.write(json.dumps(s, ensure_ascii=False) + "\n")
                    rejected_total += 1

    total = kept_total + rejected_total
    rate = rejected_total / total if total else 0
    print(f"[dedup] 保留 {kept_total} / 淘汰 {rejected_total}（淘汰率 {rate:.1%}，阈值 {threshold}，seed {seed}）")
    return kept_total, rejected_total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rejected", default=None)
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--seed", type=int, default=42, help="组内洗牌种子（铁律 7）")
    parser.add_argument("--embed-url", default=None, help="远程 embedding API base_url（如 http://localhost:8097）")
    args = parser.parse_args()

    out = Path(args.output)
    rej = Path(args.rejected) if args.rejected else out.parent / "rejected_dedup.jsonl"
    filter_dedup(
        input_path=Path(args.input),
        output_path=out,
        rejected_path=rej,
        threshold=args.threshold,
        embed_url=args.embed_url,
        seed=args.seed,
    )
