"""
阶段 2.1：Group A — 模板化切分

每条条文生成 5 个指令样本（5 模板轮换），输出 ~8755 条。
无 LLM 调用，纯本地。

运行：python -m src.synth.group_a [--smoke]
  --smoke  仅处理前 20 条，用于冒烟测试
"""
import argparse
import json
import re
import hashlib
from html.parser import HTMLParser
from pathlib import Path


# ─── HTML 表格 → Markdown ──────────────────────────────────────────────────

class _TableParser(HTMLParser):
    """解析单个 <table>...</table> 块，构建行列二维结构。"""

    def __init__(self):
        super().__init__()
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell_text: str | None = None
        self._cell_attrs: dict = {}
        self._in_table = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell_text = ""
            self._cell_attrs = attrs_d

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._row is not None and self._cell_text is not None:
            self._row.append(
                {
                    "text": re.sub(r"\s+", " ", self._cell_text).strip(),
                    "rowspan": int(self._cell_attrs.get("rowspan", 1)),
                    "colspan": int(self._cell_attrs.get("colspan", 1)),
                }
            )
            self._cell_text = None

    def handle_data(self, data):
        if self._cell_text is not None:
            self._cell_text += data

    def handle_entityref(self, name):
        if self._cell_text is not None:
            self._cell_text += f"&{name};"

    def handle_charref(self, name):
        if self._cell_text is not None:
            self._cell_text += f"&#{name};"


def _build_grid(rows: list[list[dict]]) -> list[list[str]]:
    """将带 rowspan/colspan 的行列表展开为纯二维网格。"""
    occupied: dict[tuple[int, int], str] = {}
    for r_idx, row in enumerate(rows):
        c_cursor = 0
        for cell in row:
            while (r_idx, c_cursor) in occupied:
                c_cursor += 1
            text = cell["text"]
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    occupied[(r_idx + dr, c_cursor + dc)] = text if dr == 0 and dc == 0 else ""
            c_cursor += cell["colspan"]

    if not occupied:
        return []

    max_r = max(r for r, _ in occupied) + 1
    max_c = max(c for _, c in occupied) + 1
    return [
        [occupied.get((r, c), "") for c in range(max_c)]
        for r in range(max_r)
    ]


def html_table_to_markdown(html: str) -> str:
    """把 <table>...</table> HTML 字符串转为 Markdown 表格。失败时原样返回。"""
    try:
        parser = _TableParser()
        parser.feed(html)
        grid = _build_grid(parser.rows)
        if not grid:
            return html

        def _fmt(row: list[str]) -> str:
            cells = [c.replace("|", "｜") for c in row]
            return "| " + " | ".join(cells) + " |"

        lines = [_fmt(grid[0])]
        lines.append("| " + " | ".join(["---"] * len(grid[0])) + " |")
        for row in grid[1:]:
            lines.append(_fmt(row))
        return "\n".join(lines)
    except Exception:
        return html


def convert_text_tables(text: str) -> str:
    """将 text 中所有 <table>...</table> 原地替换为 Markdown 表格。"""
    def _replace(m: re.Match) -> str:
        return html_table_to_markdown(m.group(0))

    return re.sub(r"<table[\s\S]*?</table>", _replace, text, flags=re.IGNORECASE)


# ─── 问题模板 ─────────────────────────────────────────────────────────────

_STANDARD_SHORT = {
    "GB50010-2010": "混凝土结构设计规范",
    "GB50011-2010": "建筑抗震设计规范",
    "GB50007-2011": "建筑地基基础设计规范",
    "GB50009-2012": "建筑结构荷载规范",
    "JGJ3-2010": "高层建筑混凝土结构技术规程",
}

_TEMPLATES = [
    # (question_fn, answer_prefix)
    lambda c: (
        f"请说明{c['standard_code']}第{c['clause_no']}条的规定。",
        f"根据《{c['standard_name']}》（{c['standard_code']}）第{c['clause_no']}条规定：",
    ),
    lambda c: (
        f"《{c['standard_name']}》中{_last_chapter(c)}部分，第{c['clause_no']}条是怎么规定的？",
        f"《{c['standard_name']}》（{c['standard_code']}）第{c['clause_no']}条规定如下：",
    ),
    lambda c: (
        f"根据{c['standard_code']}，第{c['clause_no']}条关于{_last_chapter(c)}的具体要求是什么？",
        f"依据《{c['standard_name']}》（{c['standard_code']}）第{c['clause_no']}条，具体要求如下：",
    ),
    lambda c: (
        f"{c['standard_name']}第{c['clause_no']}条的主要内容是什么？",
        f"《{c['standard_name']}》（{c['standard_code']}）第{c['clause_no']}条主要内容：",
    ),
    lambda c: (
        f"查询{c['standard_code']}第{c['clause_no']}条规定内容。",
        f"《{c['standard_name']}》（{c['standard_code']}）第{c['clause_no']}条原文如下：",
    ),
]


def _last_chapter(clause: dict) -> str:
    path = clause.get("chapter_path", [])
    if path:
        raw = path[-1]
        # 去掉前缀编号，如 "8.2 混凝土保护层" → "混凝土保护层"
        return re.sub(r"^[\d.]+\s*", "", raw).strip() or raw
    return clause["standard_name"]


def _make_answer(clause: dict, prefix: str) -> str:
    body = convert_text_tables(clause["text"])
    mandatory_tag = "【强制性条文】\n\n" if clause.get("is_mandatory") else ""
    return f"{prefix}\n\n{mandatory_tag}{body}"


# ─── 主逻辑 ───────────────────────────────────────────────────────────────

def _sample_id(group: str, clause_id: str, tpl_idx: int) -> str:
    h = hashlib.md5(f"{clause_id}_{tpl_idx}".encode()).hexdigest()[:8]
    return f"{group}_{h}"


def build_group_a(
    clauses_path: Path,
    output_dir: Path,
    smoke: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / "train.jsonl"

    clauses: list[dict] = []
    with open(clauses_path, encoding="utf-8") as f:
        for line in f:
            clauses.append(json.loads(line))
    if smoke:
        clauses = clauses[:20]

    samples: list[dict] = []
    for clause in clauses:
        for tpl_idx, tpl_fn in enumerate(_TEMPLATES):
            question, prefix = tpl_fn(clause)
            answer = _make_answer(clause, prefix)
            samples.append(
                {
                    "sample_id": _sample_id("a", clause["clause_id"], tpl_idx),
                    "group": "a",
                    "conversations": [
                        {"from": "human", "value": question},
                        {"from": "gpt", "value": answer},
                    ],
                    "meta": {
                        "source_clauses": [clause["clause_id"]],
                        "sample_type": "raw_text",
                        "synth_model": "template",
                        "template_idx": tpl_idx,
                        "quality_score": None,
                        "filters_passed": [],
                    },
                }
            )

    with open(out_file, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # manifest
    import hashlib as _hlib
    prompt_hash = _hlib.md5(repr(_TEMPLATES).encode()).hexdigest()[:12]
    manifest = {
        "group": "a",
        "version": "v1",
        "total": len(samples),
        "clauses_source": str(clauses_path),
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "template_count": len(_TEMPLATES),
        "template_hash": prompt_hash,
        "seed": None,
        "smoke": smoke,
    }
    with open(output_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[group_a] 生成 {len(samples)} 条 → {out_file}")
    print(f"[group_a] manifest → {output_dir / 'manifest.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="仅处理前 20 条")
    args = parser.parse_args()

    root = Path(__file__).parents[2]
    build_group_a(
        clauses_path=root / "data/interim/clauses.jsonl",
        output_dir=root / "data/processed/group_a",
        smoke=args.smoke,
    )
