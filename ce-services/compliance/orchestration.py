"""合规编排 —— 项目级合规检查端到端流水线（被合规服务 /compliance 使用）。

原 ce-code/service/orchestration.py 平移而来，编排逻辑（参数提取 → 查询矩阵 →
并行检索 → 逐维度去重判定 → 反思校验）**逐字不变**，唯一改动是检索来源：

  重构前：进程内 ``from retrieval.engine import search`` 直调；
  重构后：``common.knowledge_client.search`` 打知识服务 :8100 /search。

行为等价由 knowledge_client 保证：知识服务 /search 内部按
``bm25_top_k = vector_top_k = top_k*2`` 调 ``retrieval.engine.search``，与重构前
本文件直调的参数（top_k=15, bm25/vector_top_k=30, skip_rerank=True）逐字一致。

漏强条=事故：这条确定性流水线必须 server 端可控，不下放自由 agent 推理。
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from common import knowledge_client
from common.config import LLM_MODEL_ID, LLM_URL
from compliance.params import extract_params
from compliance.queries import gen_queries

logger = logging.getLogger("services.compliance")

TOP_K = 15          # 每个维度检索条款数
MAX_WORKERS = 4     # 并行线程数（检索 + 判定共用）

DISCLAIMER = "以上结果仅供参考，不替代具有执业资格的注册工程师专业审查。"


# ---------------------------------------------------------------------------
# 检索函数（绑定 standard 的闭包，HTTP 调知识服务 /search）
# ---------------------------------------------------------------------------

def _get_retrieve_fn(standard: str):
    def _retrieve(query: str) -> list[dict]:
        # 行为等价于重构前进程内 search(top_k=15, bm25/vector_top_k=30, skip_rerank=True)
        result = knowledge_client.search(
            query=query,
            standard=standard,
            top_k=TOP_K,
            skip_rerank=True,
        )
        return result.get("clauses", [])

    return _retrieve


# ---------------------------------------------------------------------------
# 步骤 1/2：参数提取 + 查询矩阵（直接复用 compliance.params / compliance.queries）
# ---------------------------------------------------------------------------

def step_extract_params(description: str, llm_url: str, model_id: str) -> dict[str, Any]:
    return extract_params(description, llm_url, model_id)


def step_gen_queries(params: dict[str, Any]) -> list[dict[str, str]]:
    return gen_queries(params)


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
    standard: str = "gb50016",
    llm_url: str = LLM_URL,
    model_id: str = LLM_MODEL_ID,
    skip_reflection: bool = False,
) -> dict[str, Any]:
    # 1. 参数提取
    logger.info("① 提取项目参数...")
    params = step_extract_params(description, llm_url, model_id)
    logger.info("  建筑类别：%s", params.get("building_category") or "待推断")
    if params.get("ambiguities"):
        logger.info("  模糊参数：%s", params["ambiguities"])

    # 2. 查询矩阵
    logger.info("② 生成查询矩阵...")
    queries = step_gen_queries(params)
    query_order = [q["dimension"] for q in queries]
    logger.info("  共 %d 个维度：%s", len(queries), query_order)

    # 3. 并行检索（各维度独立，HTTP 调知识服务 /search）
    logger.info("③ 并行检索（%d 个查询，%d 线程）...", len(queries), MAX_WORKERS)
    retrieve_fn = _get_retrieve_fn(standard)
    clauses_by_dim = step_parallel_retrieve(queries, retrieve_fn)
    total_mandatory = sum(
        sum(1 for c in clauses if c.get("is_mandatory"))
        for clauses in clauses_by_dim.values()
    )
    logger.info("  各维度检索完成，强条总计（含重复）%d 条", total_mandatory)

    # 4. 按维度并行判定
    logger.info("④ 按维度合规判定（%d 个维度，串行去重）...", len(queries))
    judgment = step_judgment(params, clauses_by_dim, query_order, llm_url, model_id)

    # 5. 反思校验
    missed: list[str] = []
    if not skip_reflection:
        logger.info("⑤ 反思校验...")
        covered = [d["dimension"] for d in judgment.get("dimensions", [])]
        missed = step_reflection(params, covered, llm_url, model_id)
        if missed:
            logger.info("  检测到可能遗漏的维度：%s", missed)
        else:
            logger.info("  维度覆盖完整，无遗漏")

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
