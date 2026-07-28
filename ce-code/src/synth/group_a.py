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
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.fingerprint import clauses_fingerprint


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

# 单条条文的字数上限——A/B 两组共用同一口径（group_b 从这里 import）。
#
# B 组因 vLLM max_model_len=32768 必须设闸，A 组不调 LLM 本可不设。但两组若口径不同，
# 那 3 条纯查表型巨表（GB50009 附录E 全国雪压风压表 11.5 万字 / GB50007 附录K 4 万字 /
# GB50009 附录A 2.4 万字）就只进 A 组不进 B 组，制造出一个**不属于设计变量的差异**：
# 消融要测的是"模板复制 vs LLM 反向生成"这一策略差异，不是"谁有上下文限制"。
#
# 且那些样本本身是坏的：E.5 生成的答案 61,704 字，训练 cutoff_len=2048 只吃得下前 5%，
# 砍在表格某行的数字中间，模型学到的是"列一张表然后戛然而止"。
# 故 A 组同样设闸——既对齐口径，又顺带去掉 15 条注定被截断的样本。
MAX_CLAUSE_CHARS = 20000


def _sample_id(group: str, clause_id: str, tpl_idx: int) -> str:
    h = hashlib.md5(f"{clause_id}_{tpl_idx}".encode()).hexdigest()[:8]
    return f"{group}_{h}"


# 模板渲染出来的固定样张——_template_hash 的输入。
_HASH_FIXTURE = {
    "standard_code": "GB50010-2010",
    "standard_name": "混凝土结构设计规范",
    "clause_no": "8.2.1",
    "chapter_path": ["8 构造规定", "8.2 混凝土保护层"],
}


def _template_hash() -> str:
    """对模板**渲染结果**取 hash，作为 A 组的可溯源指纹。

    不能用 `md5(repr(_TEMPLATES))`：模板是 lambda，repr 出来是
    `<function <lambda> at 0x1023f8cc0>`——内存地址。实测同一份代码连跑三次得到
    三个不同的 hash，既检测不出模板措辞的改动，又在什么都没改时天天变，
    对「四组是否用了同一套模板」这个溯源目的完全无效。

    改为用固定样张渲染全部模板并 hash 其输出：措辞一改 hash 就变，
    不改则跨进程、跨机器恒定。

    Args:
        无

    Returns:
        12 位十六进制指纹
    """
    rendered = "\n".join(
        "|".join(tpl_fn(_HASH_FIXTURE)) for tpl_fn in _TEMPLATES
    )
    return hashlib.md5(rendered.encode()).hexdigest()[:12]


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

    # 超长条款闸：与 B 组同口径（理由见 MAX_CLAUSE_CHARS）。
    # 按 CLAUDE.md §6.6 留痕，不静默丢弃。
    oversized = [c for c in clauses if len(c["text"]) > MAX_CLAUSE_CHARS]
    if oversized:
        failed_dir = output_dir.parents[1] / "interim/failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        skip_path = failed_dir / "group_a_oversized.jsonl"
        with open(skip_path, "w", encoding="utf-8") as f:
            for c in oversized:
                f.write(json.dumps(
                    {"clause_id": c["clause_id"], "char_len": len(c["text"]),
                     "reason": "oversized_skip"}, ensure_ascii=False) + "\n")
        print(f"[group_a] 跳过超长条款 {len(oversized)} 条（>{MAX_CLAUSE_CHARS} 字）→ {skip_path}")
        for c in oversized:
            print(f"           {c['clause_id']}  {len(c['text'])} 字")
        clauses = [c for c in clauses if len(c["text"]) <= MAX_CLAUSE_CHARS]

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
    manifest = {
        "group": "a",
        "version": "v1",
        "total": len(samples),
        # 存相对路径：绝对路径在 Mac 与服务器上不同，会让两边的 manifest 无法比对
        "clauses_source": str(clauses_path.relative_to(clauses_path.parents[2])),
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "clauses_fingerprint": clauses_fingerprint(),
        "template_count": len(_TEMPLATES),
        "template_hash": _template_hash(),
        "oversized_skipped": [
            {"clause_id": c["clause_id"], "char_len": len(c["text"])} for c in oversized
        ],
        "max_clause_chars": MAX_CLAUSE_CHARS,
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
