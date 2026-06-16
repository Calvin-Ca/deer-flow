"""Phase C 第一步：从节点树 chunks.json 抽「清单项规范」入 bill_spec。

GB/T 50854 每节那张 6 列表（项目编码/项目名称/项目特征/计量单位/工程量计算规则/
工作内容）= 一张关系表，每行即一个清单项。本模块双出口分流：

  - 表头含「项目编码」  → 清单项表 → bill_spec.jsonl（每行表 → 一条记录）
  - 表头不含「项目编码」→ 辅助/参数表（土分类表/工作面宽度表…）→ aux_tables.jsonl
    （列头异构不归一化，原样保留矩形 body + caption + provenance，供 calc_rule 查表）

非 9 位编码的行落 anomalies（人工抽查），不进 bill_spec。

输入：data/structured/chunks.json（splitter 产物）
输出：data/structured/bill_spec.jsonl + aux_tables.jsonl + 终端质量 report
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "structured" / "chunks.json"
DEFAULT_OUTDIR = ROOT / "data" / "structured"

# 清单项表的判别列头（出现即视为清单项规范表）
SPEC_HEADER_KEY = "项目编码"
# 6 列受控表头（用于按名定位列，不假定固定列序）
SPEC_COLUMNS = ["项目编码", "项目名称", "项目特征", "计量单位", "工程量计算规则", "工作内容"]
# 列名别名（措施项目 R 附录用「单位」；按 7 列含描述的用「项目特征描述」）→ 归一到标准列名
COLUMN_ALIASES = {
    "计量单位": ["计量单位", "单位"],
    "项目特征": ["项目特征", "项目特征描述"],
}
# 9 位全国统一编码
CODE_RE = re.compile(r"^\d{9}$")
# 编号拆项：在「数字 + . 或 、」前切，覆盖 "1.材料品种2.密实度" / 含换行
NUMBERED_SPLIT_RE = re.compile(r"(?=\d+[.、])")
NUMBERED_PREFIX_RE = re.compile(r"^\d+[.、]\s*")
# 受控计量单位（出现表外单位 → 抽查）
KNOWN_UNITS = {
    "m", "m²", "m2", "m³", "m3", "t", "kg", "个", "项", "处", "套", "组",
    "樘", "根", "块", "台", "座", "段", "株", "扇", "副", "对", "份", "孔", "榀",
    "100m", "10m³",
}
# 辅助表粗分类的 caption 关键词
AUX_KIND_RULES = [
    ("classification", re.compile(r"分类|类别")),
    ("parameter", re.compile(r"宽度|系数|计算|厚度|高度|含量|增加")),
]
# 规范号归一：原始 standard_id（= 文件名 basename，如 "GB_T50854-2024_房屋…"）→
# (doc_id, canonical spec_version)，让导入端不再从文件名猜版本。键为 5 位规范数字。
SPEC_REGISTRY = {
    "50500": ("GB-50500", "GB 50500-2024"),
    "50854": ("GB-50854", "GB/T 50854-2024"),
    "50856": ("GB-50856", "GB/T 50856-2024"),
}
DEFAULT_DOC = ("GB-50854", "GB/T 50854-2024")


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_chunks(path: Path) -> list[dict]:
    """读节点树 chunks.json。

    参数：path —— chunks.json 路径。
    返回：节点 dict 列表。
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------

def resolve_columns(header: list[str]) -> dict[str, int]:
    """把表头行解析成「标准列名 → 下标」（含别名归一，不假定固定列序）。

    参数：header —— 表头行（已 strip 的字符串列表）。
    返回：{标准列名: 下标}，仅含本表实际出现的列（缺列不入）。
    """
    col: dict[str, int] = {}
    for name in SPEC_COLUMNS:
        for alias in COLUMN_ALIASES.get(name, [name]):
            if alias in header:
                col[name] = header.index(alias)
                break
    return col


def is_spec_table(table: dict) -> bool:
    """判定一张表是否为清单项规范表（表头行含「项目编码」）。

    参数：table —— chunk.tables 中的一张表（含 body 二维数组）。
    返回：True=清单项表（走 bill_spec），False=辅助表（走 aux_tables）。
    """
    body = table.get("body") or []
    if not body:
        return False
    return SPEC_HEADER_KEY in [str(c).strip() for c in body[0]]


def split_numbered(cell: str) -> list[str]:
    """把带编号的单元格文本拆成项列表。

    "1.材料品种2.密实度" → ["材料品种", "密实度"]；"土类别" → ["土类别"]；空 → []。

    参数：cell —— 单元格原文（项目特征 / 工作内容列）。
    返回：去编号前缀、去空白后的项列表。
    """
    text = (cell or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in NUMBERED_SPLIT_RE.split(text)]
    items = [NUMBERED_PREFIX_RE.sub("", p).strip() for p in parts if p.strip()]
    return [it for it in items if it]


def chapter_of(chunk: dict) -> str:
    """取节点所属分部（溯源 / 归类用）。

    参数：chunk —— 节点 dict。
    返回：祖先链根标题，无祖先则用自身 title。
    """
    anc = chunk.get("ancestor_titles") or []
    return (anc[0] if anc else chunk.get("title", "")).strip()


def normalize_spec(standard_id: str) -> tuple[str, str]:
    """原始 standard_id → (doc_id, canonical spec_version)。

    从 standard_id 里抠 5 位规范数字（如 "GB_T50854-2024_房屋…" → "50854"）查 SPEC_REGISTRY；
    查不到则退回默认（GB/T 50854-2024），避免把文件名当版本号写进库。

    参数：standard_id —— chunk.standard_id（splitter 透传的文件名 basename）。
    返回：(doc_id, spec_version) 二元组。
    """
    m = re.search(r"(50500|50854|50856)", standard_id or "")
    if m:
        return SPEC_REGISTRY[m.group(1)]
    return DEFAULT_DOC


def classify_aux(caption: str) -> str:
    """按 caption 关键词粗分辅助表类型。

    参数：caption —— 表标题（如「土分类表」「工作面宽度计算表」）。
    返回：classification / parameter / unknown。
    """
    cap = caption or ""
    for kind, pat in AUX_KIND_RULES:
        if pat.search(cap):
            return kind
    return "unknown"


def _provenance(chunk: dict, table: dict) -> dict:
    """构造可溯源指针（回指原节点 / 表 / 页）。

    参数：chunk —— 节点 dict；table —— 表 dict。
    返回：{node_path, caption, page} 溯源 dict。
    """
    return {
        "node_path": chunk.get("node_path"),
        "caption": table.get("caption"),
        "page": table.get("page"),
    }


# ---------------------------------------------------------------------------
# 抽取主逻辑
# ---------------------------------------------------------------------------

def extract(chunks: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """遍历节点树，双出口抽出清单项 / 辅助表，并收集异常行。

    参数：chunks —— chunks.json 节点列表。
    返回：(bill_specs, aux_tables, anomalies) 三个 dict 列表。
    """
    bill_specs: list[dict] = []
    aux_tables: list[dict] = []
    anomalies: list[dict] = []

    for chunk in chunks:
        chapter = chapter_of(chunk)
        doc_id, spec_version = normalize_spec(chunk.get("standard_id") or "")

        for table in chunk.get("tables") or []:
            body = table.get("body") or []
            if not body:
                continue

            if not is_spec_table(table):
                header = [str(c).strip() for c in body[0]]
                aux_tables.append({
                    "chapter": chapter,
                    "caption": table.get("caption"),
                    "kind": classify_aux(table.get("caption") or ""),
                    "header": header,
                    "body": body,
                    "provenance": _provenance(chunk, table),
                    "doc_id": doc_id,
                    "spec_version": spec_version,
                })
                continue

            # 清单项表：按列名定位下标（含别名归一，不假定固定列序）
            header = [str(c).strip() for c in body[0]]
            col = resolve_columns(header)
            code_i = col[SPEC_HEADER_KEY]

            for row in body[1:]:
                code = str(row[code_i]).strip() if len(row) > code_i else ""
                if not CODE_RE.match(code):
                    # 表头重复行 / 续表头 / 合并残留 → 异常，不入库
                    anomalies.append({
                        "chapter": chapter,
                        "node_path": chunk.get("node_path"),
                        "caption": table.get("caption"),
                        "raw_code": code,
                        "row": row,
                    })
                    continue

                def cell(name: str) -> str:
                    """按列名取单元格文本（缺列 / 越界返回空串）。

                    参数：name —— 列名。返回：单元格 strip 后文本。
                    """
                    i = col.get(name)
                    if i is None or i >= len(row):
                        return ""
                    return str(row[i]).strip()

                bill_specs.append({
                    "code": code,
                    "name": cell("项目名称"),
                    "unit": cell("计量单位"),
                    "feature_schema": split_numbered(cell("项目特征")),
                    "calc_rule": cell("工程量计算规则"),
                    "work_content": split_numbered(cell("工作内容")),
                    "chapter": chapter,
                    "doc_id": doc_id,
                    "spec_version": spec_version,
                    "provenance": _provenance(chunk, table),
                })

    return bill_specs, aux_tables, anomalies


# ---------------------------------------------------------------------------
# 质量 report
# ---------------------------------------------------------------------------

def report(bill_specs: list[dict], aux_tables: list[dict], anomalies: list[dict]) -> None:
    """打印质量门禁报告（数量 / 编码唯一性 / 连续性 / 单位 / 特征空率 / 辅助表清单）。

    参数：bill_specs / aux_tables / anomalies —— extract 三出口。
    返回：无（终端打印）。
    """
    # 总览
    t = Table(title="抽取总览", show_header=False)
    t.add_row("清单项(bill_spec)", str(len(bill_specs)))
    t.add_row("辅助表(aux_tables)", str(len(aux_tables)))
    t.add_row("异常行(丢弃)", str(len(anomalies)))
    console.print(t)

    # 编码唯一性
    codes = [b["code"] for b in bill_specs]
    dups = [c for c, n in Counter(codes).items() if n > 1]
    console.print(f"[bold]编码唯一性[/]：去重前 {len(codes)}，去重后 {len(set(codes))}"
                  + (f"，[red]重复 {len(dups)}：{dups[:10]}[/]" if dups else "，[green]无重复[/]"))

    # 同分部编码连续性（断号告警，可能漏行）
    by_ch: dict[str, list[str]] = defaultdict(list)
    for b in bill_specs:
        by_ch[b["chapter"]].append(b["code"])
    gaps = []
    for ch, cs in by_ch.items():
        nums = sorted(int(c) for c in cs)
        for a, b in zip(nums, nums[1:]):
            if 1 < b - a <= 5:  # 小跳跃疑似漏行（大跳为正常换子目）
                gaps.append((ch, f"{a:09d}→{b:09d}"))
    if gaps:
        console.print(f"[yellow]编码小幅断号 {len(gaps)} 处（疑似漏行，抽查）[/]：{gaps[:8]}")
    else:
        console.print("[green]编码连续性：无可疑小幅断号[/]")

    # 单位受控
    bad_units = Counter(b["unit"] for b in bill_specs if b["unit"] not in KNOWN_UNITS)
    if bad_units:
        console.print(f"[yellow]表外单位 {sum(bad_units.values())} 行[/]：{dict(bad_units.most_common(10))}")
    else:
        console.print("[green]计量单位：全部受控[/]")

    # 特征列空率：区分「措施项目类」（特征+规则皆空，正常）与「疑漏行」（特征空但有规则）
    no_feat = [b for b in bill_specs if not b["feature_schema"]]
    measure_like = [b for b in no_feat if not b["calc_rule"]]
    suspect = [b for b in no_feat if b["calc_rule"]]
    console.print(f"特征项为空：{len(no_feat)}/{len(bill_specs)} 行"
                  f"（措施项目类无特征 {len(measure_like)}，正常）"
                  + (f"[yellow]；疑漏行 {len(suspect)}（特征空但有计算规则，抽查）："
                     f"{[b['code'] for b in suspect[:8]]}[/]" if suspect else ""))

    # 辅助表清单
    at = Table(title="辅助表清单（人工核对分类）")
    at.add_column("kind"); at.add_column("caption"); at.add_column("chapter"); at.add_column("header")
    for a in aux_tables:
        at.add_row(a["kind"], str(a["caption"]), a["chapter"][:16], " | ".join(a["header"]))
    console.print(at)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（一行一条记录，UTF-8 不转义）。

    参数：records —— 记录列表；path —— 输出路径。
    返回：无。
    """
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path),
              default=DEFAULT_INPUT, show_default=True, help="chunks.json 路径")
@click.option("--outdir", type=click.Path(path_type=Path), default=DEFAULT_OUTDIR,
              show_default=True, help="bill_spec.jsonl / aux_tables.jsonl 输出目录")
@click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
def main(input_path: Path, outdir: Path, dry_run: bool) -> None:
    """chunks.json → bill_spec.jsonl + aux_tables.jsonl（双出口）+ 质量 report。"""
    chunks = load_chunks(input_path)
    bill_specs, aux_tables, anomalies = extract(chunks)
    report(bill_specs, aux_tables, anomalies)

    if dry_run:
        console.print("[dim]--dry-run：未落盘[/]")
        return

    outdir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(bill_specs, outdir / "bill_spec.jsonl")
    _write_jsonl(aux_tables, outdir / "aux_tables.jsonl")
    if anomalies:
        _write_jsonl(anomalies, outdir / "bill_spec_anomalies.jsonl")
    console.print(f"[green]已写[/] {outdir/'bill_spec.jsonl'}（{len(bill_specs)}）、"
                  f"{outdir/'aux_tables.jsonl'}（{len(aux_tables)}）"
                  + (f"、anomalies（{len(anomalies)}）" if anomalies else ""))


if __name__ == "__main__":
    main()
