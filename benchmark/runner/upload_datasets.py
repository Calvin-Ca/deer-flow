"""任务2a：把 benchmark 金标集上传成 Langfuse Dataset。

把 ``benchmark/routing_eval/agent_routing_eval.jsonl`` 灌进 Langfuse Datasets，
每条用例 → 一个 dataset item（input=问法，expected_output=期望路由/澄清/版本，
metadata=用例号/能力/分组/判读提示）。上传后即可用 ``run_routing_experiment.py``
对同一数据集反复跑实验、在 UI 里按 variant/model 横向比。

幂等：``create_dataset`` 同名复用；item 以原始用例 id 作 dataset item id，重复上传
覆盖同 id 而非堆叠。

运行（服务器上）：
    uv run --project backend python benchmark/runner/upload_datasets.py
可选 --only routing 仅传路由集（当前只接了 routing，retrieval/agent_eval 待扩）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402

# 项目根 = benchmark/runner/ 的上两级。
_ROOT = Path(__file__).resolve().parents[2]

ROUTING_DATASET = "agent-routing-eval"
ROUTING_JSONL = _ROOT / "benchmark" / "routing_eval" / "agent_routing_eval.jsonl"


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


def upload_routing(client) -> int:
    """上传路由评测集到 Langfuse Dataset。

    功能：见模块 docstring；映射 query→input、{route,clarify,gold}→expected_output。
    参数：client —— Langfuse 客户端。
    返回：写入的 item 条数。
    """
    if not ROUTING_JSONL.exists():
        raise SystemExit(f"找不到金标集：{ROUTING_JSONL}")

    client.create_dataset(
        name=ROUTING_DATASET,
        description="Agent 路由/红线评测（AGENT_INTEGRATION_DEV §0 升级判定门）。"
        "input=用户问法；expected_output 含 expect_route/expect_clarify/gold。",
        metadata={"source": "benchmark/routing_eval/agent_routing_eval.jsonl"},
    )

    rows = _read_jsonl(ROUTING_JSONL)
    for r in rows:
        client.create_dataset_item(
            dataset_name=ROUTING_DATASET,
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
    print(f"✓ 已上传 {len(rows)} 条到 Langfuse Dataset «{ROUTING_DATASET}»")
    return len(rows)


def main() -> int:
    """按 --only 选择上传哪些金标集。

    功能：见模块 docstring。
    参数：无（命令行 --only）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="上传 benchmark 金标到 Langfuse Dataset")
    parser.add_argument("--only", choices=["routing"], default=None, help="只传指定集（默认全部已接入的）")
    args = parser.parse_args()

    client = require_langfuse()
    if args.only in (None, "routing"):
        upload_routing(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
