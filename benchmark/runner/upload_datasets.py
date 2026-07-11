"""任务2a：把 benchmark 金标集上传成 Langfuse Dataset。

把 ``benchmark/routing_eval/agent_routing_eval.jsonl`` 灌进 Langfuse Datasets，
每条用例 → 一个 dataset item（input=问法，expected_output=期望路由/澄清/版本，
metadata=用例号/能力/分组/判读提示）。上传后即可用 ``run_routing_experiment.py``
对同一数据集反复跑实验、在 UI 里按 variant/model 横向比。

本脚本在全链路中的位置（``[本地]``=在 runner 进程内算、不碰 Langfuse；``[LF]``=Langfuse
服务端执行/存储）。可见 **upload_datasets 只是其中「[LF 写] 登记靶子」这一步**——判分与
跑 agent 全在本地，Langfuse 只当账本：

    benchmark/routing_eval/agent_routing_eval.jsonl          [本地] 金标源文件
        │  upload_datasets.py（本脚本）：读 jsonl → create_dataset_item
        ▼
    Langfuse Dataset「agent-routing-eval」(items)            [LF 写] 登记靶子
        │  run_routing_experiment.py：get_dataset().items    [LF 读] 拉回用例
        ▼
    跑 agent（DeerFlowClient 调模型/工具、旁观工具调用）       [本地] 驱动 + 观测
        ▼
    本地判分 route_ok / clarify_ok（纯 Python 对金标）         [本地] 判官（非 Langfuse）
        │  create_score / dataset_run_items.create           [LF 写] 挂分 + 挂进 run
        ▼
    Datasets → Runs → lead_v1（看板）                        [LF UI] 逐用例 × 跨 run 比

幂等：``create_dataset`` 同名复用；item 以原始用例 id 作 dataset item id，重复上传
覆盖同 id 而非堆叠。

运行（服务器上）：
    uv run --project backend python benchmark/runner/upload_datasets.py
可选 --only 仅传指定集（缺省全部）：routing 路由 / bill_match_routing 清单匹配路由扩充 /
clist 清单匹配召回 / toolcall 工具调用 /
cost_task 端到端组价 / norm_faithful 规范忠实度 / clause 条文召回。agent_eval 三子集当前
多为 sample 模板，按「先接管道、数据后补」上传（优先真金标 .jsonl，回退 .sample.jsonl）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402

# 项目根 = benchmark/runner/ 的上两级。
_ROOT = Path(__file__).resolve().parents[2]

ROUTING_DATASET = "agent-routing-eval"
ROUTING_JSONL = _ROOT / "benchmark" / "routing_eval" / "agent_routing_eval.jsonl"

# 清单匹配意图路由扩充集（100 条，query 全部锚定 retrieval_eval 真实金标，由
# routing_eval/gen_bill_match_routing.py 确定性生成）。独立 dataset、不并入冻结基线，
# 跑法：run_routing_experiment.py --dataset bill-match-routing。
BILL_MATCH_ROUTING_DATASET = "bill-match-routing"
BILL_MATCH_ROUTING_JSONL = _ROOT / "benchmark" / "routing_eval" / "bill_match_routing.jsonl"

# 清单匹配（描述→9位清单码）：2013/2024 两份金标合进一个 dataset，spec 进 input/metadata。
# 不含 match_gold_2013_uncovered.jsonl —— 那是「库未覆盖码」清单（{code9,name}，无 query），
# 量的是知识层覆盖缺口、非检索召回，不进逐条召回评测。
CLIST_DATASET = "clist-match-eval"
CLIST_GOLD = {
    "2024": _ROOT / "benchmark" / "retrieval_eval" / "match_gold.jsonl",
    "2013": _ROOT / "benchmark" / "retrieval_eval" / "match_gold_2013.jsonl",
}


def clist_item_id(spec: str, query: str) -> str:
    """清单匹配 dataset item 的稳定 id（上传与跑实验两处共用，保证逐条对齐）。

    功能：金标 jsonl 无自带 id，用 spec+query 的短哈希派生稳定 id——同 query 多次上传幂等
        覆盖，且 run_retrieval_experiment 据同一 query 重算即得同 id，能挂回正确 item。
    参数：spec —— 国标版本 2013/2024；query —— 构件/做法描述（金标里的 query）。
    返回：形如 ``clist-2024-1a2b3c4d5e`` 的 item id。
    """
    h = hashlib.md5(f"{spec}{query}".encode("utf-8")).hexdigest()[:10]
    return f"clist-{spec}-{h}"


def _read_jsonl(path: Path) -> list[dict]:
    """读 jsonl 为 dict 列表（跳过空行）。

    功能：金标集统一是逐行 JSON。
    参数：path —— jsonl 路径。
    返回：每行解析出的 dict 列表。
    """
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _upload_routing_file(client, jsonl, dataset_name: str, description: str) -> int:
    """上传一份路由 schema 的 jsonl 到指定 Langfuse Dataset（agent_routing_eval 与扩充集共用）。

    功能：映射 query→input、{route,clarify,gold}→expected_output、其余字段进 metadata。
    参数：client —— Langfuse 客户端；jsonl —— 金标文件路径；dataset_name/description —— 目标 dataset。
    返回：写入的 item 条数。
    """
    if not jsonl.exists():
        raise SystemExit(f"找不到金标集：{jsonl}")

    client.create_dataset(
        name=dataset_name,
        description=description,
        metadata={"source": str(jsonl.relative_to(_ROOT))},
    )

    rows = _read_jsonl(jsonl)
    for r in rows:
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=str(r["id"]),  # 以用例号作 item id → 重复上传幂等覆盖
            input={"query": r["query"]},
            expected_output={
                "expect_route": r.get("expect_route"),
                "expect_clarify": r.get("expect_clarify"),
                "gold": r.get("gold"),
            },
            metadata={
                "agent": r.get("agent"),
                "group": r.get("group"),
                "note": r.get("note"),
            },
        )
    client.flush()
    print(f"✓ 已上传 {len(rows)} 条到 Langfuse Dataset «{dataset_name}»")
    return len(rows)


def upload_routing(client) -> int:
    """上传路由评测集（冻结基线）到 Langfuse Dataset。"""
    return _upload_routing_file(
        client,
        ROUTING_JSONL,
        ROUTING_DATASET,
        "Agent 路由/红线评测（AGENT_INTEGRATION_DEV §0 升级判定门）。input=用户问法；expected_output 含 expect_route/expect_clarify/gold。",
    )


def upload_bill_match_routing(client) -> int:
    """上传清单匹配意图路由扩充集（100 条，query 锚定 retrieval_eval 真实金标）。"""
    return _upload_routing_file(
        client,
        BILL_MATCH_ROUTING_JSONL,
        BILL_MATCH_ROUTING_DATASET,
        "清单匹配意图路由评测（100 条）：真实项目特征选码/核实问法，全部应路由（expect_route=true）"
        "、全部不反问（cost 侧缺版本默认深圳·2013）。query 逐字锚定 retrieval_eval/match_gold*.jsonl。",
    )


def upload_clist(client) -> int:
    """上传清单匹配金标（2013+2024 两份）到一个 Langfuse Dataset。

    功能：把 ``match_gold.jsonl``(2024) 与 ``match_gold_2013.jsonl``(2013) 合进
        ``clist-match-eval``，query→input.description、spec 进 input/metadata、
        gold 码进 expected_output；item id 用 ``clist_item_id`` 派生（幂等 + 可被
        run_retrieval_experiment 重算对齐）。
    参数：client —— Langfuse 客户端。
    返回：写入的 item 总条数（两份合计）。
    """
    client.create_dataset(
        name=CLIST_DATASET,
        description="清单匹配评测（构件描述 → 9 位清单码）。input=描述+spec；"
        "expected_output.gold=金标码（任一命中即对）。复用 benchmark/retrieval_eval/match_gold*.jsonl。",
        metadata={"source": "benchmark/retrieval_eval/match_gold*.jsonl"},
    )
    total = 0
    for spec, path in CLIST_GOLD.items():
        if not path.exists():
            print(f"  (跳过 spec={spec}：找不到 {path.name})")
            continue
        rows = _read_jsonl(path)
        for r in rows:
            g = r.get("gold")
            gold = g if isinstance(g, list) else [g]
            client.create_dataset_item(
                dataset_name=CLIST_DATASET,
                id=clist_item_id(spec, r["query"]),
                input={"description": r["query"], "spec": spec},
                expected_output={"gold": gold},
                metadata={"spec": spec, "note": r.get("note"), "source": path.name},
            )
        total += len(rows)
        print(f"  spec={spec}: {len(rows)} 条（{path.name}）")
    client.flush()
    print(f"✓ 已上传 {total} 条到 Langfuse Dataset «{CLIST_DATASET}»")
    return total


# agent_eval 三子集 + 条文召回。**当前多为 sample 模板**（2 行）——按「数据以后再补」原则
# 先把 dataset 接进来：upload 优先读真金标 {name}.jsonl，没有才回退 {name}.sample.jsonl，
# 补好真数据（同名 .jsonl）重跑本脚本即覆盖，无需改代码。
_AGENT_EVAL = _ROOT / "benchmark" / "agent_eval"
GB50016_JSON = _ROOT / "benchmark" / "retrieval_eval" / "gb50016_eval.json"

TOOLCALL_DATASET = "agent-toolcall-eval"
COST_TASK_DATASET = "agent-cost-task-eval"
NORM_FAITHFUL_DATASET = "agent-norm-faithful-eval"
CLAUSE_DATASET = "clause-recall-eval"


def _agent_eval_gold(name: str) -> Path:
    """取 agent_eval 某子集金标路径：优先真金标，回退 sample 模板。

    功能：支持「先接管道、数据后补」——补好 {name}.jsonl 即自动取代 sample。
    参数：name —— 子集名（toolcall / cost_task / norm_faithful）。
    返回：实际存在的金标路径（真金标优先，否则 sample）。
    """
    real = _AGENT_EVAL / name / f"{name}.jsonl"
    return real if real.exists() else _AGENT_EVAL / name / f"{name}.sample.jsonl"


def upload_toolcall(client) -> int:
    """上传工具调用评测集（query → 期望 tool+args）到 Langfuse Dataset。

    功能：input=query+可用工具；expected_output=期望调用 tool/args/arg_match 口径。
        ⚠️ gold 的 tool 名须用**真实 agent 工具**（qa.py/cost.py/ce-cost_*），sample 里的
        query_bill_8100 等为 AGENT_BENCHMARK 理想名，照搬会恒不命中。
    参数：client —— Langfuse 客户端。
    返回：写入的 item 条数。
    """
    path = _agent_eval_gold("toolcall")
    if not path.exists():
        raise SystemExit(f"找不到金标：{path}")
    client.create_dataset(name=TOOLCALL_DATASET, description="工具调用评测：query→期望 tool+args（arg_match=exact|subset|semantic）。gold 的 tool 名须用真实 agent 工具名。", metadata={"source": str(path.relative_to(_ROOT))})
    rows = _read_jsonl(path)
    for r in rows:
        ec = r.get("expected_call", {})
        client.create_dataset_item(
            dataset_name=TOOLCALL_DATASET, id=str(r["id"]),
            input={"query": r["query"], "available_tools": r.get("available_tools")},
            expected_output={"tool": ec.get("tool"), "args": ec.get("args"), "arg_match": r.get("arg_match", "subset")},
            metadata={"quant_variants": r.get("quant_variants"), "pass_k": r.get("pass_k"), "split": r.get("split")},
        )
    client.flush()
    print(f"✓ 已上传 {len(rows)} 条到 Langfuse Dataset «{TOOLCALL_DATASET}»（源 {path.name}）")
    return len(rows)


def upload_cost_task(client) -> int:
    """上传端到端组价任务评测集（user_goal → 终态校验）到 Langfuse Dataset。

    功能：input=用户目标+初始上下文+工具环境；expected_output=terminal_check（定额/费率带/
        must_cite）；metadata=policy（红线）/难度。驱动 runner 待建（终态校验依赖 cost 输出 schema）。
    参数：client —— Langfuse 客户端。
    返回：写入的 item 条数。
    """
    path = _agent_eval_gold("cost_task")
    if not path.exists():
        raise SystemExit(f"找不到金标：{path}")
    client.create_dataset(name=COST_TASK_DATASET, description="端到端组价任务评测：user_goal→terminal_check（定额/费率带/must_cite）。驱动需跑完整 agent + 终态校验。", metadata={"source": str(path.relative_to(_ROOT))})
    rows = _read_jsonl(path)
    for r in rows:
        client.create_dataset_item(
            dataset_name=COST_TASK_DATASET, id=str(r["id"]),
            input={"user_goal": r["user_goal"], "init_context": r.get("init_context"), "tool_env": r.get("tool_env")},
            expected_output={"terminal_check": r.get("terminal_check")},
            metadata={"policy": r.get("policy"), "difficulty": r.get("difficulty"), "pass_k": r.get("pass_k"), "split": r.get("split")},
        )
    client.flush()
    print(f"✓ 已上传 {len(rows)} 条到 Langfuse Dataset «{COST_TASK_DATASET}»（源 {path.name}）")
    return len(rows)


def upload_norm_faithful(client) -> int:
    """上传规范问答忠实度评测集（question → gold_contexts/answer_points）到 Langfuse Dataset。

    功能：input=问题；expected_output=金标条文上下文 + 答案要点 + 是否应拒答。忠实度/相关性
        类指标走 LLM-judge（受 §6.6 SSRF 阻断），context_recall 类可确定性判（待 qa.py 支持 GB50016）。
    参数：client —— Langfuse 客户端。
    返回：写入的 item 条数。
    """
    path = _agent_eval_gold("norm_faithful")
    if not path.exists():
        raise SystemExit(f"找不到金标：{path}")
    client.create_dataset(name=NORM_FAITHFUL_DATASET, description="规范问答忠实度评测（RAGAS）：question→gold_contexts/gold_answer_points。判分走 LLM-judge / context_recall。", metadata={"source": str(path.relative_to(_ROOT))})
    rows = _read_jsonl(path)
    for r in rows:
        client.create_dataset_item(
            dataset_name=NORM_FAITHFUL_DATASET, id=str(r["id"]),
            input={"question": r["question"]},
            expected_output={"gold_contexts": r.get("gold_contexts"), "gold_answer_points": r.get("gold_answer_points"), "expect_refuse": r.get("expect_refuse")},
            metadata={"ragas_metrics": r.get("ragas_metrics"), "split": r.get("split")},
        )
    client.flush()
    print(f"✓ 已上传 {len(rows)} 条到 Langfuse Dataset «{NORM_FAITHFUL_DATASET}»（源 {path.name}）")
    return len(rows)


def upload_clause(client) -> int:
    """上传条文召回评测集（gb50016_eval.json：问题 → 应召回条文）到 Langfuse Dataset。

    功能：input=问题；expected_output=expected_clauses + must_be_mandatory；metadata=相关条文/类型。
        ⚠️ standard `gb50016` 尚不在 qa.py 支持列表（仅 50500/50854/50856），驱动 runner 待知识
        服务加载该规范后再建——本上传只先把金标纳管进 Langfuse。
    参数：client —— Langfuse 客户端。
    返回：写入的 item 条数。
    """
    if not GB50016_JSON.exists():
        raise SystemExit(f"找不到金标：{GB50016_JSON}")
    data = json.loads(GB50016_JSON.read_text(encoding="utf-8"))
    client.create_dataset(name=CLAUSE_DATASET, description="条文召回评测（GB50016）：问题→应召回条文 + 强条标志。standard 待 qa.py 支持后方可跑。", metadata={"source": "benchmark/retrieval_eval/gb50016_eval.json"})
    for r in data:
        client.create_dataset_item(
            dataset_name=CLAUSE_DATASET, id=str(r["id"]),
            input={"query": r["query"]},
            expected_output={"expected_clauses": r.get("expected_clauses"), "must_be_mandatory": r.get("must_be_mandatory")},
            metadata={"related_clauses": r.get("related_clauses"), "user_type": r.get("user_type"), "notes": r.get("notes")},
        )
    client.flush()
    print(f"✓ 已上传 {len(data)} 条到 Langfuse Dataset «{CLAUSE_DATASET}»")
    return len(data)


_UPLOADERS = {
    "routing": upload_routing,
    "bill_match_routing": upload_bill_match_routing,
    "clist": upload_clist,
    "toolcall": upload_toolcall,
    "cost_task": upload_cost_task,
    "norm_faithful": upload_norm_faithful,
    "clause": upload_clause,
}


def main() -> int:
    """按 --only 选择上传哪些金标集（缺省全部已接入的）。

    功能：见模块 docstring。
    参数：无（命令行 --only）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="上传 benchmark 金标到 Langfuse Dataset")
    parser.add_argument("--only", choices=list(_UPLOADERS), default=None, help="只传指定集（默认全部）")
    args = parser.parse_args()

    client = require_langfuse()
    targets = [args.only] if args.only else list(_UPLOADERS)
    for name in targets:
        _UPLOADERS[name](client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
