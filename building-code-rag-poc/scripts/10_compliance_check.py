"""阶段 2 核心：项目级合规检查——端到端编排。

流程：
  自由文本描述
    → 08 参数提取（Qwen3 /no_think）
    → 09 查询矩阵生成（规则）
    → 并行检索（05 retrieve × N 个维度）
    → 按维度并行判定（每维度独立调用 Qwen3，避免单次 prompt 过长）
    → 反思校验（Qwen3，检查维度遗漏）
    → 结构化合规报告

使用方式：
  .venv/bin/python scripts/10_compliance_check.py \\
    --project "地上11层住宅楼，总高32米，每层850平方米，地下一层车库" \\
    --store-dir data/vector_store/GB_50016_20142018

  .venv/bin/python scripts/10_compliance_check.py \\
    --project "..." \\
    --store-dir data/vector_store/GB_50016_20142018 \\
    --output /tmp/compliance_report.json
"""
from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import click
import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent

LLM_URL = "http://localhost:8099"
LLM_MODEL_ID = "qwen3-8b"
EMBED_URL = "http://localhost:8097"
EMBED_MODEL_ID = "/model"
MILVUS_HOST = "localhost"
MILVUS_PORT = 19530
TOP_K = 15          # 每个维度检索条款数
MAX_WORKERS = 4     # 并行线程数（检索 + 判定共用）

DISCLAIMER = "以上结果仅供参考，不替代具有执业资格的注册工程师专业审查。"


# ---------------------------------------------------------------------------
# 模块懒加载
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_retrieve_fn(store_dir: Path):
    mod = _load_module("poc_retrieve", ROOT / "scripts" / "05_retrieve.py")
    collection = f"building_code_{store_dir.name}".lower().replace("-", "_")

    def _retrieve(query: str) -> list[dict]:
        return mod.retrieve(
            query=query,
            store_dir=store_dir,
            milvus_host=MILVUS_HOST,
            milvus_port=MILVUS_PORT,
            collection_name=collection,
            embed_url=EMBED_URL,
            embed_model_id=EMBED_MODEL_ID,
            top_k=TOP_K,
            bm25_top_k=TOP_K * 2,
            vector_top_k=TOP_K * 2,
            skip_rerank=True,
        )

    return _retrieve


# ---------------------------------------------------------------------------
# 步骤 1：参数提取
# ---------------------------------------------------------------------------

def step_extract_params(description: str, llm_url: str, model_id: str) -> dict[str, Any]:
    mod = _load_module("poc_extract", ROOT / "scripts" / "08_extract_params.py")
    return mod.extract_params(description, llm_url, model_id)


# ---------------------------------------------------------------------------
# 步骤 2：查询矩阵
# ---------------------------------------------------------------------------

def step_gen_queries(params: dict[str, Any]) -> list[dict[str, str]]:
    mod = _load_module("poc_gen_queries", ROOT / "scripts" / "09_gen_queries.py")
    return mod.gen_queries(params)


# ---------------------------------------------------------------------------
# 步骤 3：并行检索（按维度，保留各维度独立结果）
# ---------------------------------------------------------------------------

def step_parallel_retrieve(
    queries: list[dict[str, str]],
    retrieve_fn,
) -> dict[str, list[dict]]:
    """返回 {dimension: clauses_list}，每维度独立保留检索结果。"""
    results_by_dim: dict[str, list[dict]] = {}

    def _run(item: dict[str, str]) -> tuple[str, list[dict]]:
        return item["dimension"], retrieve_fn(item["query"])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_run, q): q["dimension"] for q in queries}
        for future in as_completed(futures):
            dimension, clauses = future.result()
            results_by_dim[dimension] = clauses

    return results_by_dim


# ---------------------------------------------------------------------------
# 步骤 4：按维度并行判定
# ---------------------------------------------------------------------------

DIM_JUDGMENT_SYSTEM = """\
你是建筑规范合规判定专家，严格基于 GB 50016-2014(2018)。

任务：对给定维度的检索结果，筛选出适用条款并给出合规状态。
输出合法 JSON，不输出任何 JSON 以外的文字。

合规状态：
- "符合"：项目参数已明确满足该条款要求
- "不符合"：项目参数明确违反该条款要求
- "需核实"：需图纸或现场数据确认（最常见）
- "需补充信息"：项目描述缺少必要参数，无法判定
- "不适用"：该条款与本项目无关（排除，不输出）

/no_think"""


def _judge_one_dimension(
    dimension: str,
    params: dict[str, Any],
    clauses: list[dict],
    llm_url: str,
    model_id: str,
    seen_paths: set[str],
) -> dict[str, Any]:
    """单维度合规判定，跳过 seen_paths 中已在其他维度报告过的条款。"""
    # 只取强条，去掉已在其他维度出现过的
    mandatory = [
        c for c in clauses
        if c.get("is_mandatory") and c.get("clause_path") not in seen_paths
    ]
    if not mandatory:
        return {"dimension": dimension, "clauses": []}

    params_summary = (
        f"建筑类别：{params.get('building_category') or params.get('building_type', '未知')}\n"
        f"高度：{params.get('height_m', '未知')}米，"
        f"地上{params.get('floors_above_ground', '?')}层，"
        f"地下{params.get('floors_underground', 0)}层\n"
        f"标准层面积：{params.get('floor_area_m2', '未知')}m²\n"
        f"特殊用途：{params.get('special_uses') or '无'}"
    )

    clause_lines = []
    for c in mandatory:
        content_snippet = (c.get("content") or "")[:150].replace("\n", " ")
        clause_lines.append(f"[{c['clause_path']}] {content_snippet}")

    user_msg = (
        f"项目参数：\n{params_summary}\n\n"
        f"当前维度：{dimension}\n"
        f"以下强条共 {len(mandatory)} 条，请筛选适用项并判定合规状态：\n\n"
        + "\n".join(clause_lines)
        + '\n\n输出 JSON：\n'
        '{"clauses": [{"clause": "条款号", "text": "关键原文（≤80字）", '
        '"is_mandatory": true, "compliance_status": "状态", "note": "简短说明"}]}'
    )

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": DIM_JUDGMENT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(f"{llm_url}/v1/chat/completions", json=payload, timeout=120)
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("```")).strip()

    result = json.loads(raw)
    clauses_out = [c for c in result.get("clauses", []) if c.get("compliance_status") != "不适用"]

    # 记录已报告的条款
    for c in clauses_out:
        seen_paths.add(c.get("clause", ""))

    return {"dimension": dimension, "clauses": clauses_out}


def step_judgment(
    params: dict[str, Any],
    clauses_by_dim: dict[str, list[dict]],
    query_order: list[str],
    llm_url: str,
    model_id: str,
) -> dict[str, Any]:
    """按维度并行判定，全局去重避免同一条款在多个维度重复出现。"""
    seen_paths: set[str] = set()
    dimensions: list[dict] = []

    # 按查询顺序串行（保证去重一致性）；如需加速可改为并行但需加锁
    for dimension in query_order:
        clauses = clauses_by_dim.get(dimension, [])
        dim_result = _judge_one_dimension(
            dimension, params, clauses, llm_url, model_id, seen_paths
        )
        if dim_result["clauses"]:
            dimensions.append(dim_result)

    uncertain = params.get("ambiguities") or []
    return {"dimensions": dimensions, "uncertain_params": uncertain}


# ---------------------------------------------------------------------------
# 步骤 5：反思校验
# ---------------------------------------------------------------------------

REFLECTION_SYSTEM = """\
你是建筑规范审核专家。根据建筑参数和已覆盖的合规维度，判断是否有重要维度被遗漏。
输出合法 JSON，不输出任何 JSON 以外的文字。
/no_think"""

REQUIRED_DIMENSIONS = [
    "建筑分类与耐火等级", "防火间距", "防火分区", "安全出口",
    "疏散楼梯", "疏散距离与走道宽度", "消防车道", "建筑构件耐火极限",
    "室内消火栓", "自动喷水灭火系统", "火灾自动报警系统",
]


def step_reflection(
    params: dict[str, Any],
    covered_dimensions: list[str],
    llm_url: str,
    model_id: str,
) -> list[str]:
    user_msg = (
        f"项目参数：{json.dumps(params, ensure_ascii=False)}\n\n"
        f"已覆盖的合规维度：{covered_dimensions}\n\n"
        f"参考维度清单（不限于此）：{REQUIRED_DIMENSIONS}\n\n"
        "请列出可能被遗漏的重要合规维度（仅列出与本项目相关且尚未覆盖的）。"
        "若无遗漏输出空列表。\n\n"
        '输出 JSON：{"missed_dimensions": ["维度名1", "维度名2"]}'
    )

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": REFLECTION_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(f"{llm_url}/v1/chat/completions", json=payload, timeout=60)
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("```")).strip()

    return json.loads(raw).get("missed_dimensions", [])


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compliance_check(
    description: str,
    store_dir: Path,
    llm_url: str = LLM_URL,
    model_id: str = LLM_MODEL_ID,
    skip_reflection: bool = False,
) -> dict[str, Any]:
    # 1. 参数提取
    console.print("[bold]① 提取项目参数...[/bold]")
    params = step_extract_params(description, llm_url, model_id)
    console.print(f"  建筑类别：{params.get('building_category') or '待推断'}")
    if params.get("ambiguities"):
        console.print(f"  [yellow]模糊参数：{params['ambiguities']}[/yellow]")

    # 2. 查询矩阵
    console.print("[bold]② 生成查询矩阵...[/bold]")
    queries = step_gen_queries(params)
    query_order = [q["dimension"] for q in queries]
    console.print(f"  共 {len(queries)} 个维度：{query_order}")

    # 3. 并行检索（各维度独立）
    console.print(f"[bold]③ 并行检索（{len(queries)} 个查询，{MAX_WORKERS} 线程）...[/bold]")
    retrieve_fn = _get_retrieve_fn(store_dir)
    clauses_by_dim = step_parallel_retrieve(queries, retrieve_fn)
    total_mandatory = sum(
        sum(1 for c in clauses if c.get("is_mandatory"))
        for clauses in clauses_by_dim.values()
    )
    console.print(f"  各维度检索完成，强条总计（含重复）{total_mandatory} 条")

    # 4. 按维度并行判定
    console.print(f"[bold]④ 按维度合规判定（{len(queries)} 个维度，串行去重）...[/bold]")
    judgment = step_judgment(params, clauses_by_dim, query_order, llm_url, model_id)

    # 5. 反思校验
    missed: list[str] = []
    if not skip_reflection:
        console.print("[bold]⑤ 反思校验...[/bold]")
        covered = [d["dimension"] for d in judgment.get("dimensions", [])]
        missed = step_reflection(params, covered, llm_url, model_id)
        if missed:
            console.print(f"  [yellow]检测到可能遗漏的维度：{missed}[/yellow]")
        else:
            console.print("  [green]维度覆盖完整，无遗漏[/green]")

    # 6. 组装报告
    dimensions = judgment.get("dimensions", [])
    mandatory_total = sum(len(d.get("clauses", [])) for d in dimensions)

    return {
        "project_description": description,
        "project_params": params,
        "building_category": params.get("building_category"),
        "dimensions": dimensions,
        "mandatory_clauses_total": mandatory_total,
        "uncertain_params": judgment.get("uncertain_params", []),
        "missed_dimensions_warning": missed,
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_report(report: dict) -> None:
    console.print(Panel(
        f"[bold]{report.get('building_category', '未知类别')}[/bold]\n"
        f"强条共 {report['mandatory_clauses_total']} 条",
        title="合规检查报告",
        border_style="green",
    ))

    for dim in report.get("dimensions", []):
        t = Table(
            title=f"[cyan]{dim['dimension']}[/cyan]",
            show_header=True,
            header_style="bold",
            show_lines=True,
        )
        t.add_column("条款号", style="cyan", width=10)
        t.add_column("合规状态", width=12)
        t.add_column("说明", no_wrap=False, max_width=55)
        for c in dim.get("clauses", []):
            status = c.get("compliance_status", "")
            color = {
                "符合": "green", "不符合": "red",
                "需核实": "yellow", "需补充信息": "yellow", "不适用": "dim",
            }.get(status, "white")
            note = c.get("note") or (c.get("text") or "")[:60]
            t.add_row(c.get("clause", ""), f"[{color}]{status}[/{color}]", note)
        console.print(t)

    if report.get("uncertain_params"):
        console.print(f"\n[yellow]⚠ 需补充参数：{report['uncertain_params']}[/yellow]")
    if report.get("missed_dimensions_warning"):
        console.print(f"[yellow]⚠ 可能遗漏的维度：{report['missed_dimensions_warning']}[/yellow]")
    console.print(f"\n[dim]{report['disclaimer']}[/dim]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--project", "-p", required=True, help="项目自由文本描述。")
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="向量索引目录（data/vector_store/<standard>/）。",
)
@click.option("--llm-url", default=LLM_URL, show_default=True)
@click.option("--llm-model-id", default=LLM_MODEL_ID, show_default=True)
@click.option("--skip-reflection", is_flag=True, help="跳过反思校验步骤（加速调试）。")
@click.option("--output", "output_path", default=None, help="报告写入 JSON 文件。")
def main(
    project: str,
    store_dir: Path,
    llm_url: str,
    llm_model_id: str,
    skip_reflection: bool,
    output_path: str | None,
) -> None:
    """项目级合规检查：参数提取 → 并行检索 → 按维度判定 → 反思校验。"""
    try:
        report = compliance_check(project, store_dir, llm_url, llm_model_id, skip_reflection)
    except requests.RequestException as e:
        console.print(f"[red]服务调用失败：{e}[/red]")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]LLM 返回了非法 JSON：{e}[/red]")
        raise SystemExit(1)

    print_report(report)

    if output_path:
        Path(output_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[green]✓ 报告已写入 {output_path}[/green]")


if __name__ == "__main__":
    main()
