"""任务2c：清单匹配评测——复用选码引擎，挂分到 Langfuse Dataset Run。

「清单匹配」= 构件描述 → 9 位清单码（先 bill_match 候选召回，再 select_code 候选内选码）。
这套评测在 ``benchmark/select_eval/tools/eval_select.py`` 里**已成熟实现**（Recall@k / 端到端
Top-1 / 候选内 Top-1 / 高置信错码=0 等红线指标）——原属 ce-services，随 ce-services 退役已迁入
本仓 ``benchmark/select_eval/``（self-contained 选码引擎：common/cost/tools 三包）。本脚本**不
重写检索/选码**，只在 backend venv 下把 select_eval 加进 path、调它的 ``run_eval`` 拿逐条 + 汇总，
再加 **Langfuse 层**：逐条建 trace、关联进 dataset run、挂 ``match_top1`` / ``recalled`` 两分。
运行时仍需 :8100 知识服务 + :8099 vLLM。指标口径见 eval_select 与 AGENT_BENCHMARK §L3。

与 routing 实验同构（主线程逐条、不另起 loop）。检索脚本本身不产 Langfuse trace，故用
``start_as_current_observation`` 手建一条记录 query/选码/候选数，再据其 trace_id 挂分。

前置：先 ``upload_datasets.py --only clist`` 灌好 ``clist-match-eval`` 数据集（item id 由
``clist_item_id`` 派生，本脚本据同一 query 重算对齐）。

运行（服务器上，需 :8100 知识服务 + :8099 vLLM 在跑）：
    uv run --project backend python benchmark/runner/run_retrieval_experiment.py \
        --spec 2024 --run-name clist_2024_v1
退出码 0=完成。指标见终端表 + Langfuse UI 的 dataset run。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402
from upload_datasets import CLIST_DATASET, CLIST_GOLD, clist_item_id  # noqa: E402

# 项目根 = benchmark/runner/ 的上两级；选码引擎已迁入 benchmark/select_eval/（原 ce-services）。
_ROOT = Path(__file__).resolve().parents[2]
_SELECT_EVAL = _ROOT / "benchmark" / "select_eval"


def _import_eval_engine():
    """把 select_eval 加进 path 并导入选码评测引擎；缺路径/依赖时给可执行提示。

    功能：复用成熟选码评测——其内部用相对包导入（common/cost/tools），需把
        ``benchmark/select_eval`` 根加进 sys.path 才能 import。
    参数：无。
    返回：``(run_eval, KNOWLEDGE_URL, LLM_URL, LLM_MODEL_ID)``——评测入口 + 三个默认地址。
    异常：``SystemExit`` —— 导入失败（路径不对 / 依赖缺）时带提示退出。
    """
    if str(_SELECT_EVAL) not in sys.path:
        sys.path.insert(0, str(_SELECT_EVAL))
    try:
        from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL  # noqa: PLC0415
        from tools.eval_select import run_eval  # noqa: PLC0415

        return run_eval, KNOWLEDGE_URL, LLM_URL, LLM_MODEL_ID
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"导入选码评测引擎失败：{type(exc).__name__}: {exc}\n"
            f"  需在 backend venv（已装 requests）下跑，且 select_eval 存在于 {_SELECT_EVAL}。"
        )


def _push_one(client, run_name: str, spec: str, top_k: int, d: dict) -> None:
    """把一条 eval 明细推进 Langfuse：建 trace → 关联 dataset run → 挂两分。

    功能：检索脚本不产 trace，这里手建一条记录 query/选码/候选数，据其 trace_id 挂
        match_top1（端到端选码命中）与 recalled（金标是否进候选）。
    参数：client Langfuse 客户端；run_name dataset run 名；spec 版本；top_k 候选深度；
        d eval_select.run_eval 返回的单条明细（含 query/gold/selection/recalled/correct）。
    返回：无（副作用：写 trace + dataset_run_item + 两个 score）。
    """
    item_id = clist_item_id(spec, d["query"])
    picked = (d.get("selection") or {}).get("code")
    with client.start_as_current_observation(
        name="clist-match",
        as_type="span",
        input={"description": d["query"], "spec": spec},
        output={"picked": picked, "candidates_count": d.get("candidates_count")},
        metadata={"gold": d.get("gold"), "recalled": d.get("recalled"), "correct": d.get("correct"), "need_review": d.get("need_review")},
    ) as span:
        trace_id = span.trace_id

    client.api.dataset_run_items.create(run_name=run_name, dataset_item_id=item_id, trace_id=trace_id)
    client.create_score(name="match_top1", value=1.0 if d.get("correct") else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"选码={picked} 金标={d.get('gold')}")
    client.create_score(name="recalled", value=1.0 if d.get("recalled") else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"候选数={d.get('candidates_count')} (Recall@{top_k})")


def main() -> int:
    """跑清单匹配评测（复用 select_eval 引擎）并把逐条结果挂到 Langfuse Dataset Run。

    功能：见模块 docstring。
    参数：无（命令行 --spec / --run-name / --top-k / --knowledge-url / --llm-url）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="清单匹配评测：复用 select_eval 选码引擎并挂分到 Langfuse")
    parser.add_argument("--spec", required=True, choices=["2013", "2024"], help="国标版本（决定用哪份金标）")
    parser.add_argument("--run-name", default=None, help="dataset run 名（建议带 spec，便于横向比）")
    parser.add_argument("--top-k", type=int, default=10, help="候选召回深度（默认 10）")
    parser.add_argument("--knowledge-url", default=None, help="知识服务 :8100 地址（默认 select_eval config）")
    parser.add_argument("--llm-url", default=None, help="vLLM 地址（默认 select_eval config）")
    args = parser.parse_args()

    client = require_langfuse()
    run_eval, k_url, l_url, model_id = _import_eval_engine()

    gold_path = CLIST_GOLD[args.spec]
    if not gold_path.exists():
        raise SystemExit(f"找不到金标：{gold_path}")
    run_name = args.run_name or f"clist-{args.spec}-{uuid.uuid4().hex[:8]}"

    # 跑成熟评测：打印指标表（含 PRD 红线对照）+ 返回逐条明细 & 汇总。
    summary, details = run_eval(
        gold_path,
        args.spec,
        args.top_k,
        args.knowledge_url or k_url,
        args.llm_url or l_url,
        model_id,
    )

    # 逐条推 Langfuse。
    for d in details:
        _push_one(client, run_name, args.spec, args.top_k, d)
    client.flush()

    print("\n========== Langfuse ==========")
    print(f"run_name = {run_name}   dataset = {CLIST_DATASET}")
    print(f"Top-1(端到端) = {summary['top1']:.0%}   Recall@{args.top_k} = {summary['recall_at_k']:.0%}   高置信错码 = {summary['n_dangerous']}（须为 0）")
    print(f"逐条分数已挂到：Datasets → {CLIST_DATASET} → Runs → {run_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
