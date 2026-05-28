#!/usr/bin/env python3
"""项目级合规检查 skill——deer-flow 调用包装。

用法（通过 deer-flow agent 的 bash 工具）：
    python check.py --project "地上11层住宅楼，总高32米，每层850平方米，地下一层车库"
    python check.py --project "..." --output /tmp/compliance_report.json
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import click

_SKILL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SKILL_DIR.parent.parent.parent
_POC_SCRIPTS = _PROJECT_ROOT / "building-code-rag-poc" / "scripts"
_POC_ROOT = _PROJECT_ROOT / "building-code-rag-poc"

_VECTOR_STORE = _POC_ROOT / "data" / "vector_store"

_DEFAULTS = {
    "standard": "GB_50016-20142018",
    "llm_url": "http://localhost:8099",
    "llm_model_id": "qwen3-8b",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _exit_json(obj: dict) -> None:
    click.echo(json.dumps(obj, ensure_ascii=False, indent=2))
    raise SystemExit(1)


@click.command()
@click.option("--project", "-p", required=True, help="项目自由文本描述。")
@click.option("--standard", default=_DEFAULTS["standard"], show_default=True, help="规范代号（目前支持 GB_50016-20142018）。")
@click.option("--skip-reflection", is_flag=True, help="跳过反思校验（加速调试）。")
@click.option("--output", "output_path", default=None, help="报告写入 JSON 文件；不指定则输出到 stdout。")
def main(project: str, standard: str, skip_reflection: bool, output_path: str | None) -> None:
    """项目合规检查：自由文本描述 → 全量强条清单 + 逐条合规判定。"""

    store_dir = _VECTOR_STORE / standard
    if not store_dir.exists():
        _exit_json({
            "error": f"向量索引目录不存在: {store_dir}",
            "hint": f"请先在服务器上运行: uv run scripts/04_build_index.py",
        })

    if not _POC_SCRIPTS.exists():
        _exit_json({"error": f"POC 脚本目录不存在: {_POC_SCRIPTS}"})

    try:
        check_mod = _load_module("poc_compliance", _POC_SCRIPTS / "10_compliance_check.py")
    except Exception as exc:
        _exit_json({"error": f"模块加载失败: {exc}"})

    try:
        report = check_mod.compliance_check(
            description=project,
            store_dir=store_dir,
            llm_url=_DEFAULTS["llm_url"],
            model_id=_DEFAULTS["llm_model_id"],
            skip_reflection=skip_reflection,
        )
    except Exception as exc:
        _exit_json({"error": f"合规检查失败: {exc}"})

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")
        click.echo(f"✓ 报告已写入 {out}", err=True)
    else:
        click.echo(output)


if __name__ == "__main__":
    main()
