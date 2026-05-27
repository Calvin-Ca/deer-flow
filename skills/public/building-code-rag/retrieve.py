#!/usr/bin/env python3
"""建筑规范 RAG 检索入口——deer-flow skill 调用包装。

用法（通过 deer-flow agent 的 bash 工具）：
    python retrieve.py --query "防火墙耐火极限要求"
    python retrieve.py --query "..." --standard gb50016 --top-k 20 --output /tmp/result.json
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import click

# ── 路径解析 ──────────────────────────────────────────────────────────────────
# 本文件位于 skills/public/building-code-rag/，向上三级到 deer-flow 项目根
_SKILL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SKILL_DIR.parent.parent.parent
_POC_SCRIPTS = _PROJECT_ROOT / "building-code-rag-poc" / "scripts"
_POC_ROOT = _PROJECT_ROOT / "building-code-rag-poc"

# ── 默认配置（服务器环境）────────────────────────────────────────────────────
_VECTOR_STORE = _POC_ROOT / "data" / "vector_store"

_DEFAULTS = {
    "milvus_host": "localhost",
    "milvus_port": 19530,
    "embed_url": "http://localhost:8097",
    "embed_model_id": "/model",
    "llm_url": "http://localhost:8099",
    "llm_model_id": "qwen3-8b",
    "top_k": 20,
}

_STANDARD_ALIASES: dict[str, str] = {
    "gb50016": "GB_50016-20142018",
    "gb50016-2014": "GB_50016-20142018",
    "gb50016-20142018": "GB_50016-20142018",
    "GB_50016-20142018": "GB_50016-20142018",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _exit_json(obj: dict) -> None:
    click.echo(json.dumps(obj, ensure_ascii=False, indent=2))
    raise SystemExit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--query", "-q", required=True, help="自然语言查询。")
@click.option("--standard", default="gb50016", show_default=True, help="规范代号（目前支持 gb50016）。")
@click.option("--top-k", default=_DEFAULTS["top_k"], show_default=True, help="最终返回条款数（强条不截断）。")
@click.option("--skip-rerank", is_flag=True, help="跳过 Rerank，使用 RRF 排序（调试用）。")
@click.option("--output", "output_path", default=None, help="结果写入 JSON 文件；不指定则输出到 stdout。")
def main(
    query: str,
    standard: str,
    top_k: int,
    skip_rerank: bool,
    output_path: str | None,
) -> None:
    """建筑规范条文混合检索 + Qwen3 结构化生成。"""

    # 1. 解析规范 → store_dir
    store_name = _STANDARD_ALIASES.get(standard) or _STANDARD_ALIASES.get(standard.lower())
    if not store_name:
        _exit_json({"error": f"未知规范代号: {standard!r}，支持: {list(_STANDARD_ALIASES)}"})

    store_dir = _VECTOR_STORE / store_name
    if not store_dir.exists():
        _exit_json({
            "error": f"向量索引目录不存在: {store_dir}",
            "hint": f"请先在服务器上运行: uv run scripts/04_build_index.py --standard {standard}",
        })

    # 2. 动态加载 POC 模块（文件名以数字开头，不能直接 import）
    if not _POC_SCRIPTS.exists():
        _exit_json({"error": f"POC 脚本目录不存在: {_POC_SCRIPTS}"})

    try:
        retrieve_mod = _load_module("poc_retrieve", _POC_SCRIPTS / "05_retrieve.py")
        generate_mod = _load_module("poc_generate", _POC_SCRIPTS / "06_generate.py")
    except Exception as exc:
        _exit_json({"error": f"POC 模块加载失败: {exc}"})

    # 3. 检索
    collection = f"building_code_{store_name}".lower().replace("-", "_")
    try:
        clauses = retrieve_mod.retrieve(
            query=query,
            store_dir=store_dir,
            milvus_host=_DEFAULTS["milvus_host"],
            milvus_port=_DEFAULTS["milvus_port"],
            collection_name=collection,
            embed_url=_DEFAULTS["embed_url"],
            embed_model_id=_DEFAULTS["embed_model_id"],
            top_k=top_k,
            bm25_top_k=top_k * 2,
            vector_top_k=top_k * 2,
            skip_rerank=skip_rerank,
        )
    except Exception as exc:
        _exit_json({"error": f"检索失败: {exc}"})

    # 4. 生成
    try:
        user_msg = generate_mod.build_user_message(query, clauses)
        response = generate_mod.call_qwen3(
            generate_mod.SYSTEM_PROMPT,
            user_msg,
            _DEFAULTS["llm_url"],
            _DEFAULTS["llm_model_id"],
        )
    except Exception as exc:
        _exit_json({
            "error": f"生成失败: {exc}",
            "retrieved_clauses_count": len(clauses),
        })

    # 5. 输出
    result = {
        "query": query,
        "standard": store_name,
        "retrieved_clauses_count": len(clauses),
        "mandatory_clauses_count": sum(1 for c in clauses if c.get("is_mandatory")),
        "response": response,
    }

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")
        click.echo(f"✓ 结果已写入 {out}", err=True)
    else:
        click.echo(output)


if __name__ == "__main__":
    main()
