"""extract 编排器:把 02 产出的 v1 条款树跑完整富化链 → v2 多表征条款 + v1 兼容桥。

链路(原地富化,各 enricher 解耦、纯 stdlib):
  strength.annotate_strength      波1  modal_strength / is_mandatory_clause(黑体)
  references.annotate_references   波1  references(分型) + referenced_by(反向)
  ancestors.attach_ancestor_titles 波3 ancestor_titles(祖先标题链)
  + schema 默认补全(version/effective_date/status/applicable_scope=unknown/formulas/figures)
  + schema.to_v1_compat 桥回 is_mandatory / references_to → 04 建索引 / engine 不崩

  波2 tables(表格结构化)、波3 scope(适用范围谓词)未建 → applicable_scope 统一
  scope_status=unknown 进保守召回。

── 强条召回安全闸(领域铁律:宁可多召不可漏)──────────────────────────────────────
v1 的 is_mandatory 把语气"应"也算强制;而 to_v1_compat 的硬语气并集
只含 {必须/严禁/不应/不得},**不含"应"**。这是对的(黑体强条 ≠ 语气),但真值要
靠官方强制性条文清单(--official)兜底。**无官方清单时**直接重建索引会丢掉所有
仅含"应"的条款 → 强条召回回退。故本编排器:无 --official 时进**保守模式**,把
modal=="应" 也并回 is_mandatory(等价 v1 召回口径),并打印 v1→v2 强条差异供研判;
有 --official 时按黑体真值收紧,不再保守加"应"。

I/O:
  输入  data/structured/<safe>_clauses.json        (02 产出, v1)
  输出  data/structured/<safe>_clauses_v2.json      (v2 + v1 兼容字段)
  重建索引时让 04 指向 *_clauses_v2.json(04 已支持 --input)。

用法(单行):
  python extract/build.py --input data/structured/GB_50016_2014_2018_clauses.json
  python extract/build.py --input <v1.json> --official data/structured/gb50016_mandatory.json --version 2018 --effective-date 2018-10-01
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # ce-code/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema  # noqa: E402  (ce-code 根模块,单一真值契约)
from extract import ancestors, references, strength  # noqa: E402


def enrich(
    clauses: list[dict],
    *,
    official: set[str] | None = None,
    version: str = "",
    effective_date: str = "",
    status: str = "active",
) -> list[dict]:
    """v1 条款 list → v2 + v1 兼容桥(返回新 list,不改入参语义外的字段)。

    official 为空 → 保守模式(modal=="应" 并回 is_mandatory,等价 v1 召回口径)。
    """
    official = official or set()
    conservative = not official  # 无官方清单 → 保守保 v1 召回

    # 波1 + 波3 原地富化
    strength.annotate_strength(clauses, official)
    references.annotate_references(clauses)
    ancestors.attach_ancestor_titles(clauses)

    out: list[dict] = []
    for c in clauses:
        c["version"] = c.get("version") or version
        c["effective_date"] = c.get("effective_date") or effective_date
        c["status"] = c.get("status") or status

        scope = c.get("applicable_scope")
        if not isinstance(scope, dict) or "scope_status" not in scope:
            c["applicable_scope"] = schema.empty_scope()  # 波3 scope 未建 → unknown 保守召回

        c.setdefault("formulas", [])
        c.setdefault("figures", [])

        bridged = schema.to_v1_compat(c)  # 桥回 is_mandatory / references_to
        if conservative and c.get("modal_strength") == "应":
            # 无官方清单时,语气"应"并回强条,保证重建索引前强条召回不回退(宁可多召)
            bridged["is_mandatory"] = True
        out.append(bridged)

    return out


def _diff_mandatory(v1: list[dict], v2: list[dict]) -> tuple[int, int, list[str]]:
    """返回 (v1 强条数, v2 强条数, v1 是强条但 v2 不是的 clause_path)。后者非空=召回回退。"""
    v2_by_path = {c["clause_path"]: c for c in v2}
    v1_n = sum(1 for c in v1 if c.get("is_mandatory"))
    v2_n = sum(1 for c in v2 if c.get("is_mandatory"))
    lost = [
        c["clause_path"]
        for c in v1
        if c.get("is_mandatory") and not v2_by_path.get(c["clause_path"], {}).get("is_mandatory")
    ]
    return v1_n, v2_n, lost


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="把 v1 条款树富化为 v2 多表征条款(+ v1 兼容桥)。")
    p.add_argument("--input", required=True, type=Path, help="02 产出的 <safe>_clauses.json")
    p.add_argument("--output", type=Path, default=None, help="默认同目录 <safe>_clauses_v2.json")
    p.add_argument("--official", type=Path, default=None, help="官方强制性条文清单 JSON([\"3.1.2\",...])")
    p.add_argument("--version", default="", help="规范版本,如 2018")
    p.add_argument("--effective-date", default="", help="生效日期,如 2018-10-01")
    p.add_argument("--status", default="active", help="active/superseded/abolished")
    args = p.parse_args(argv)

    if not args.input.exists():
        print(f"[错误] 输入不存在: {args.input}", file=sys.stderr)
        return 1

    with open(args.input, encoding="utf-8") as f:
        clauses = json.load(f)

    official = strength.load_official(args.official)
    if args.official and not official:
        print(f"[警告] --official 指定的清单为空或不存在: {args.official}", file=sys.stderr)

    # 深拷贝一份原始 is_mandatory 供 v1→v2 差异对照(enrich 会原地改 clauses)
    v1_snapshot = [{"clause_path": c["clause_path"], "is_mandatory": c.get("is_mandatory", False)} for c in clauses]

    out = enrich(
        clauses,
        official=official,
        version=args.version,
        effective_date=args.effective_date,
        status=args.status,
    )

    v1_n, v2_n, lost = _diff_mandatory(v1_snapshot, out)
    mode = "黑体真值(官方清单)" if official else "保守(无清单,语气'应'并回)"
    refs_n = sum(len(c.get("references", [])) for c in out)
    cross_n = sum(1 for c in out for r in c.get("references", []) if r.get("type") == "cross_standard")

    print(f"条款总数      : {len(out)}")
    print(f"强条口径      : {mode}")
    print(f"强条数 v1→v2  : {v1_n} → {v2_n}")
    print(f"引用边总数    : {refs_n}(含跨规范 {cross_n})")
    if lost:
        print(f"[红线告警] v1 是强条但 v2 不是,共 {len(lost)} 条(可能漏强条): {lost[:20]}", file=sys.stderr)
        print("           若无官方清单,本不应出现;请勿用此输出重建索引。", file=sys.stderr)

    out_path = args.output or args.input.with_name(args.input.stem.replace("_clauses", "") + "_clauses_v2.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✓ 已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
