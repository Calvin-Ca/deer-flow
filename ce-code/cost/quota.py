"""Phase C：从深圳消耗量标准（SJG 171/170）节点树抽定额三表（组价主体）。

SJG 消耗量标准的定额子目表是**转置矩阵**：一张表承载一组定额子目，
列 = 子目（如 010001-1 / 010001-2），行 = 属性 + 工料机。例：

    caption: 工作内容：… 单位：10m³
    子目编号 | 010001-1 | 010001-2 | 参考价格
    子目名称 | 实心砖基础（两子目共享） | 变体名（干混/湿拌砌筑砂浆）
    费用构成: 综合单价 / 人工费 / 材料费 / 机械费 / 管理费 / 利润 / 安文 / 规费 / 税金
    工料机:   人工(元) / 材料(实物含量) / 机械(台班含量) —— 每列一个值 + 参考价格列

矩形化（FormatAdapter 已展开 colspan/rowspan）后**值的列位仍不固定**（「其中」组多一级
标签列、值右移一格）。故按**单位格锚定**：每行第一个「数值 / 破折号」前一格是单位，其后
N 格（N=子目数）即各子目的值。

三出口（与 schema.sql 对应）：
  - quota_item：每子目一条（编号/名称/单位/工作内容 + 人工费/材料费/机械费/综合单价）
  - resource：人材机主数据（去重）
  - quota_resource：子目 × 资源的含量（natural key 链接，load_pg 解析成 id）

价格列（2023-08 参考价）按决策**跳过**（价格主源走信息价月刊 SZ-JGXX-PRICE 独立管道）。
非定额子目表（系数表/厚度表等）→ aux_tables.jsonl（复用 bill_spec 的辅助表口径）。

输入：SJG 的 chunks.json（splitter 产物）
输出：data/structured/quota_item.jsonl + resource.jsonl + quota_resource.jsonl + aux 复用
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = ROOT / "data" / "structured"

# 定额子目编号：6 位 + 「-序号」（如 010001-1）
CODE_RE = re.compile(r"^\d{6}-\d+$")
# 纯数值单元（含小数）
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
# 破折号（不适用）的各种写法
DASHES = {"—", "－", "-", "–"}
# 定额表判别：表头首格
QUOTA_HEADER = "子目编号"
# 工料机段分隔行首格
RESOURCE_SEP = "工料机名称"
# 费用构成行 → quota_item 字段（按 label 关键词匹配，取每子目值）
COST_FIELDS = {
    "labor_cost": "人工费",
    "material_cost": "材料费",
    "machine_cost": "机械费",
}
BASE_PRICE_LABEL = "参考综合单价"  # 取「2023年8月参考综合单价」作 base_price（不含税综合单价）
# 工料机类别归一（col0 的 rowspan 标签）
CATEGORY_MAP = {"人工费": "人工", "人工": "人工", "材料": "材料", "机械": "机械"}


def load_chunks(path: Path) -> list[dict]:
    """读 SJG 的 chunks.json。

    参数：path —— chunks.json 路径。
    返回：节点 dict 列表。
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def caption_text(table: dict) -> str:
    """把 table.caption（可能是 str / list）拼成单串。

    参数：table —— chunk.tables 的一张表。
    返回：caption 文本（空则空串）。
    """
    cap = table.get("caption")
    if isinstance(cap, list):
        return " ".join(str(x) for x in cap)
    return str(cap or "")


def _clean_unit(u: str) -> str:
    """清洗 MinerU 的 LaTeX 单位串。

    "$10\\mathrm{m}^3$" → "10m³"。

    参数：u —— 原始单位串。
    返回：清洗后单位。
    """
    u = u.replace("$", "").replace("\\mathrm", "").replace("\\mathbf", "").replace("\\,", "")
    u = u.replace("{", "").replace("}", "")
    u = u.replace("^3", "³").replace("^2", "²")
    return u.strip()


def parse_caption(caption: str) -> tuple[str, str]:
    """从 caption 抽「工作内容」与「单位」。

    "工作内容：清理基槽坑… 单位：$10\\mathrm{m}^3$" → ("清理基槽坑…", "10m³")。

    参数：caption —— 表标题文本。
    返回：(work_content, unit)，缺失项为空串。
    """
    work = re.search(r"工作内容[:：]\s*(.+?)(?:\s*单位[:：]|$)", caption)
    unit = re.search(r"单位[:：]\s*(\S+)", caption)
    return (work.group(1).strip() if work else "",
            (_clean_unit(unit.group(1)) if unit else ""))


def is_quota_table(table: dict) -> bool:
    """判定是否为定额子目表（表头首格含「子目编号」）。

    参数：table —— chunk.tables 的一张表。
    返回：True=定额子目表，False=辅助表。
    """
    body = table.get("body") or []
    return bool(body) and QUOTA_HEADER in str((body[0] or [""])[0])


def _denoise_num(cell: str) -> str:
    """去掉数字的千分位噪声（空格 / 不间断空格 / 逗号）。

    MinerU 常把 "12 869.54" 渲染成带空格 → 归一为 "12869.54"。

    参数：cell —— 单元格文本。
    返回：去噪后字符串。
    """
    return (cell or "").replace(" ", "").replace(" ", "").replace(",", "").strip()


def _is_value(cell: str) -> bool:
    """单元格是否为「子目值」（数值或破折号）。

    参数：cell —— 单元格文本。
    返回：True=数值/破折号（锚定起点），False=标签/单位/空。
    """
    c = _denoise_num(cell)
    return (cell or "").strip() in DASHES or bool(NUM_RE.match(c))


def _anchor(row: list[str], n: int) -> tuple[list[str], str, list[str]] | None:
    """按「单位格锚定」拆一行为 (标签链, 单位, N 个子目值)。

    定位行内第一个数值/破折号单元为值起点 j：单位 = 第 j-1 格，值 = 第 j..j+n-1 格，
    标签 = 第 0..j-2 格里的非空唯一项。

    参数：row —— 矩形行；n —— 子目数。
    返回：(labels, unit, values) 或 None（行内无数值=非数据行）。
    """
    j = next((i for i, c in enumerate(row) if _is_value(c)), None)
    if j is None or j == 0:
        return None
    unit = (row[j - 1] or "").strip()
    labels = [c.strip() for c in row[:j - 1] if c and c.strip()]
    values = [(c or "").strip() for c in row[j:j + n]]
    return labels, unit, values


def _num(v: str) -> float | None:
    """子目值 → float；破折号/空 → None。

    参数：v —— 子目值单元。
    返回：float 或 None。
    """
    v = _denoise_num(v)
    return float(v) if NUM_RE.match(v) else None


def _forward_fill(cells: list[str]) -> list[str]:
    """前向填充空单元（colspan 共享名展开后仅首格有值、后格空 → 继承前值）。

    ["水玻璃", "", "耐酸沥青", ""] → ["水玻璃", "水玻璃", "耐酸沥青", "耐酸沥青"]。

    参数：cells —— 子目编号列位上的名称单元。
    返回：前向填充后的等长列表。
    """
    out, last = [], ""
    for c in cells:
        c = (c or "").strip()
        if c:
            last = c
        out.append(last)
    return out


def parse_quota_table(table: dict, chapter: str, doc_id: str, spec_version: str) -> dict:
    """解析一张定额子目表 → {items, resources, links}（转置矩阵 → 三表）。

    参数：table —— 定额子目表（body 已矩形化）；chapter —— 所属章；
          doc_id/spec_version —— 治理字段。
    返回：{"items": [...], "resources": [...], "links": [...]}（natural key 链接）。
    """
    body = table.get("body") or []
    work_content, unit = parse_caption(caption_text(table))

    # 1. 子目编号（保持列序 + 列位）→ N 个 quota_item 骨架
    code_cols = [i for i, c in enumerate(body[0]) if CODE_RE.match((c or "").strip())]
    codes = [body[0][i].strip() for i in code_cols]
    n = len(codes)
    items = {code: {"quota_code": code, "name": "", "unit": unit,
                    "work_content": work_content, "base_price": None,
                    "labor_cost": None, "material_cost": None, "machine_cost": None,
                    "chapter": chapter, "doc_id": doc_id, "spec_version": spec_version,
                    "region": "深圳", "effective_priority": 1,
                    "provenance": {"caption": caption_text(table), "page": table.get("page")}}
             for code in codes}
    if not n:
        return {"items": [], "resources": [], "links": []}

    # 2/3. 逐行扫：子目名称（按列累积）、费用构成、工料机
    name_parts: dict[str, list[str]] = {code: [] for code in codes}
    resources: dict[tuple, dict] = {}
    links: list[dict] = []
    in_resource = False
    for row in body[1:]:
        head = (row[0] or "").strip()
        if head == RESOURCE_SEP:
            in_resource = True
            continue
        if head == "子目名称":
            # 多层名称行（如 水玻璃耐酸混凝土 / 厚度T(mm) / T=60）：按子目编号列位取值，
            # 前向填充（处理 colspan 共享名：仅首格有值、后格空 → 继承），逐列累积成完整名
            vals = _forward_fill([row[i] if i < len(row) else "" for i in code_cols])
            for code, v in zip(codes, vals):
                if v and (not name_parts[code] or name_parts[code][-1] != v):
                    name_parts[code].append(v)
            continue

        parsed = _anchor(row, n)
        if parsed is None:
            continue
        labels, u, values = parsed

        if not in_resource:
            # 费用构成行 → quota_item 字段；用最具体标签 labels[-1] 避免 rowspan 父标签子串误命中
            last = labels[-1] if labels else ""
            if last.endswith("参考综合单价") and "全费用" not in last:
                for code, v in zip(codes, values):
                    items[code]["base_price"] = _num(v)
            for field, kw in COST_FIELDS.items():
                if last == kw:
                    for code, v in zip(codes, values):
                        items[code][field] = _num(v)
            continue

        # 工料机行 → resource + quota_resource（含量）
        category = CATEGORY_MAP.get(labels[0], labels[0]) if labels else ""
        name = labels[-1] if labels else ""
        rkey = (category, name, u)
        resources.setdefault(rkey, {"category": category, "name": name, "spec": None,
                                    "unit": u, "doc_id": doc_id})
        for code, v in zip(codes, values):
            amt = _num(v)
            if amt is not None:  # 跳过破折号（该子目不用此资源）
                links.append({"quota_code": code, "doc_id": doc_id, "category": category,
                              "resource_name": name, "resource_unit": u,
                              "consumption": amt})

    # 各名称行累积 → 完整 name（schema 仅一列）：实心砖基础 干混砌筑砂浆 / 水玻璃耐酸混凝土 厚度T(mm) T=60
    for code in codes:
        items[code]["name"] = re.sub(r"\s+", " ", " ".join(name_parts[code])).strip()

    return {"items": list(items.values()), "resources": list(resources.values()),
            "links": links}


def extract(chunks: list[dict]) -> dict:
    """遍历节点树，抽定额三表 + 辅助表。

    参数：chunks —— SJG chunks.json 节点列表。
    返回：{"items","resources","links","aux"}（resources 全局去重）。
    """
    items: list[dict] = []
    resources: dict[tuple, dict] = {}
    links: list[dict] = []
    aux: list[dict] = []

    for chunk in chunks:
        anc = chunk.get("ancestor_titles") or []
        chapter = (anc[0] if anc else chunk.get("title", "")).strip()
        sid = chunk.get("standard_id") or ""
        doc_id, spec_version = _normalize_sjg(sid)

        for table in chunk.get("tables") or []:
            if not (table.get("body")):
                continue
            if is_quota_table(table):
                r = parse_quota_table(table, chapter, doc_id, spec_version)
                items.extend(r["items"])
                links.extend(r["links"])
                for res in r["resources"]:
                    resources[(res["category"], res["name"], res["unit"])] = res
            else:
                aux.append({"chapter": chapter, "caption": caption_text(table),
                            "body": table.get("body"),
                            "doc_id": doc_id, "spec_version": spec_version})

    return {"items": items, "resources": list(resources.values()),
            "links": links, "aux": aux}


# SJG 规范号归一
_SJG_REGISTRY = {
    "171": ("SZ-SJG171", "SJG 171-2024"),
    "170": ("SZ-SJG170", "SJG 170-2024"),
}


def _normalize_sjg(standard_id: str) -> tuple[str, str]:
    """SJG standard_id → (doc_id, canonical spec_version)。

    参数：standard_id —— chunk.standard_id（文件名 basename）。
    返回：(doc_id, spec_version)；按建筑(171)/土石方(170)关键词或编号判别。
    """
    if "171" in standard_id or "建筑工程" in standard_id:
        return _SJG_REGISTRY["171"]
    if "170" in standard_id or "土石方" in standard_id or "地基" in standard_id:
        return _SJG_REGISTRY["170"]
    return ("SZ-SJG171", "SJG 171-2024")


def report(data: dict) -> None:
    """打印抽取总览 + 抽样，供人工核验。

    参数：data —— extract 产出。
    返回：无（终端打印）。
    """
    t = Table(title="定额抽取总览", show_header=False)
    t.add_row("定额子目 quota_item", str(len(data["items"])))
    t.add_row("资源 resource（去重）", str(len(data["resources"])))
    t.add_row("含量 quota_resource", str(len(data["links"])))
    t.add_row("辅助表 aux", str(len(data["aux"])))
    console.print(t)

    miss_cost = [i["quota_code"] for i in data["items"]
                 if i["labor_cost"] is None and i["material_cost"] is None]
    if miss_cost:
        console.print(f"[yellow]无人工/材料费的子目 {len(miss_cost)}：{miss_cost[:8]}[/]")
    cats = {}
    for r in data["resources"]:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    console.print(f"资源类别分布：{cats}")


def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（一行一条，UTF-8 不转义）。

    参数：records —— 记录列表；path —— 输出路径。
    返回：无。
    """
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@click.command()
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path),
              required=True, help="SJG chunks.json 路径")
@click.option("--outdir", type=click.Path(path_type=Path), default=DEFAULT_OUTDIR,
              show_default=True, help="三表 jsonl 输出目录")
@click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
def main(input_path: Path, outdir: Path, dry_run: bool) -> None:
    """SJG chunks.json → quota_item / resource / quota_resource jsonl + 质量 report。"""
    data = extract(load_chunks(input_path))
    report(data)
    if dry_run:
        console.print("[dim]--dry-run：未落盘[/]")
        return
    outdir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(data["items"], outdir / "quota_item.jsonl")
    _write_jsonl(data["resources"], outdir / "resource.jsonl")
    _write_jsonl(data["links"], outdir / "quota_resource.jsonl")
    console.print(f"[green]已写[/] quota_item({len(data['items'])}) / "
                  f"resource({len(data['resources'])}) / quota_resource({len(data['links'])})")


if __name__ == "__main__":
    main()
