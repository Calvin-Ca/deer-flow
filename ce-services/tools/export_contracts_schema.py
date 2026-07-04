#!/usr/bin/env python3
"""导出组价 HITL 契约 JSON Schema（M1 契约线）—— 供前端 codegen 替换手写 types.ts。

用法（ce-services 根）：
  python -m tools.export_contracts_schema                 # 打印到 stdout
  python -m tools.export_contracts_schema --out ../frontend/src/core/cost/contracts.schema.json

前端侧（下一步落地）：
  pnpm dlx json-schema-to-typescript contracts.schema.json > types.gen.ts
之后 ``core/cost/types.ts`` 改为 re-export 生成类型，手写镜像退役——改契约只动
``cost/contracts.py`` 一处，Schema/TS 全部跟着生成（单一事实源）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost.contracts import COST_INTERRUPT_ADAPTER, CostSessionResponse  # noqa: E402


def build_schema() -> dict:
    """组装导出 Schema：会话响应为根（内联三型闸），并单列 CostInterrupt 便于前端窄化。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CE Cost HITL Contracts",
        "description": "组价 HITL 对外契约（单一源 cost/contracts.py 生成，勿手改）",
        "$defs": {
            "CostSessionResponse": CostSessionResponse.model_json_schema(
                ref_template="#/$defs/CostSessionResponse/$defs/{model}"),
            "CostInterrupt": COST_INTERRUPT_ADAPTER.json_schema(
                ref_template="#/$defs/CostInterrupt/$defs/{model}"),
        },
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="导出组价 HITL 契约 JSON Schema")
    parser.add_argument("--out", help="输出文件路径（缺省打印 stdout）")
    args = parser.parse_args()

    text = json.dumps(build_schema(), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"已写出 {args.out}（{len(text)} 字符）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
