"""诊断：统计解析失败样本中有多少能被 jsonx 的转义修复捞回。

用途——在决定「是否值得补跑」之前先量化收益。写成脚本而非命令行 one-liner，
是因为要匹配的正是反斜杠本身，内联到 shell 里会被多层转义吃掉
（实测一条判断 LaTeX 占比的内联正则被 shell 吞成了 9%，而真实占比远高于此）。

用法：
    python scripts/check_parse_failures.py                       # 自动取最新失败日志
    python scripts/check_parse_failures.py data/interim/failed/20260728_parse_failed.jsonl
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT))

from src.utils import jsonx


def main() -> None:
    """统计失败样本的可恢复比例并按原因分类。

    Args:
        无（可选从 sys.argv[1] 指定日志路径）

    Returns:
        None（结果打印到标准输出）
    """
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        cands = sorted(glob.glob(str(_ROOT / "data/interim/failed/*_parse_failed.jsonl")))
        if not cands:
            print("找不到解析失败日志")
            return
        path = Path(cands[-1])

    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    print(f"日志: {path.name}   失败 {len(rows)} 条\n")

    recovered = truncated = other = 0
    for r in rows:
        raw = r.get("raw", "")
        if jsonx.extract(raw, kind="array") is not None or \
           jsonx.extract(raw, kind="object") is not None:
            recovered += 1
        elif not raw.rstrip().endswith(("]", "}", "```")):
            truncated += 1          # 输出被 max_tokens 截断，修转义救不回
        else:
            other += 1

    n = len(rows) or 1
    print(f"  ✅ 转义修复可捞回 : {recovered:>4}  ({recovered/n:.0%})")
    print(f"  ✂️ 输出被截断     : {truncated:>4}  ({truncated/n:.0%})  ← 需调高 max_tokens")
    print(f"  ❓ 其他           : {other:>4}  ({other/n:.0%})")
    print()
    if recovered:
        print(f"→ 补跑这 {len(rows)} 条条文可回收约 {recovered * 4} 条样本（每条文 4 视角）")


if __name__ == "__main__":
    main()
