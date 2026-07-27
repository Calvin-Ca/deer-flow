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

    # 日志按日期命名且**追加**写入，一天内多次跑（全量 + 若干次 resume）会堆在同一文件里，
    # 新旧混杂。且早先的 raw 被裁成 1000 字符——裁剪后的片段本就不是完整 JSON，
    # 拿它判「能否解析」必然高估失败。故先按有无 raw_len 分开：
    # 只有完整那批的结论可信，裁剪那批仅作历史存量报出、不参与判断。
    full = [r for r in rows if r.get("raw_len") is not None]
    clipped = [r for r in rows if r.get("raw_len") is None]

    def _split(rs: list[dict]) -> tuple[list, list]:
        """按 jsonx 能否解析把失败样本分成可捞回与不可捞回两组。

        Args:
            rs: 失败日志行

        Returns:
            (可捞回, 不可捞回) 两个列表
        """
        ok, bad = [], []
        for r in rs:
            raw = r.get("raw", "")
            hit = (jsonx.extract(raw, kind="array") is not None
                   or jsonx.extract(raw, kind="object") is not None)
            (ok if hit else bad).append(r)
        return ok, bad

    if clipped:
        print(f"[历史存量] {len(clipped)} 条 raw 被裁剪过（旧版留痕，只存前 1000 字符）")
        print("           片段本就不完整，无法据此判断可否捞回——已排除，不参与下面的统计。")
        print("           这些条文若仍缺样本，用 --resume 重跑即可产出完整日志。\n")

    if not full:
        print("本日志无完整留痕记录，无可分析项。")
        return

    ok, bad = _split(full)
    n = len(full)
    print(f"[可分析] {n} 条完整留痕")
    print(f"  ✅ 转义修复可捞回 : {len(ok):>4}  ({len(ok)/n:.0%})")
    print(f"  ❌ 仍解析不了     : {len(bad):>4}  ({len(bad)/n:.0%})")
    if ok:
        print(f"\n→ 补跑可回收约 {len(ok) * 4} 条样本（每条文 4 视角）")
    if bad:
        print("\n仍解析不了的样本（逐条列出，供人工判因）：")
        for r in bad[:10]:
            raw = r.get("raw", "")
            print(f"  ─ {r.get('clause_id','?')}   输出 {r.get('raw_len')} 字")
            print(f"    开头: {raw[:70]!r}")
            print(f"    结尾: {raw[-70:]!r}")
        if len(bad) > 10:
            print(f"  …另有 {len(bad) - 10} 条")


if __name__ == "__main__":
    main()
