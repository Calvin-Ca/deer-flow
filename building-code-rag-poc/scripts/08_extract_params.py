"""阶段 2 第一步：从自由文本提取结构化建筑参数。

使用 Qwen3-8B（/no_think 模式）解析用户描述，输出结构化 JSON。

使用方式：
  .venv/bin/python scripts/08_extract_params.py \\
    --description "地上11层住宅楼，总高32米，每层850平方米，地下一层车库"

  .venv/bin/python scripts/08_extract_params.py \\
    --description "..." --output /tmp/params.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import requests
from rich.console import Console

console = Console()

LLM_URL = "http://localhost:8099"
LLM_MODEL_ID = "qwen3-8b"

SYSTEM_PROMPT = """\
你是一名建筑规范专家助手。请从用户的项目描述中提取结构化参数，输出合法 JSON，不输出任何 JSON 以外的文字。

字段说明（无法确定的字段填 null，模糊信息填入 ambiguities）：
- building_type: 建筑用途，如"住宅"/"办公"/"商业"/"工业"/"仓库"/"综合体"
- building_category: 按 GB 50016 第 5.1.1 条推断，如"一类高层住宅"/"二类高层住宅"/"多层住宅"/"一类高层公共建筑"/"二类高层公共建筑"/"多层公共建筑"；无法推断填 null
- height_m: 建筑高度（米，数字）
- floors_above_ground: 地上层数（整数）
- floors_underground: 地下层数（整数，无地下室填 0）
- floor_area_m2: 标准层建筑面积（平方米，数字）
- total_area_m2: 总建筑面积（平方米；可由层数×层面积推算，无法确定填 null）
- fire_resistance_grade: 耐火等级（"一级"/"二级"/"三级"/"四级"，未说明填 null）
- location: 建设地点类型（"城镇"/"乡村"/"工业区"，未说明默认"城镇"）
- special_uses: 特殊用途列表，如 ["地下车库","商业裙房","人员密集场所"]
- adjacent_buildings: 相邻建筑简描，如 [{"type":"商业","floors":4}]，无说明填 []
- ambiguities: 影响合规判定但描述中模糊或缺失的信息列表

建筑分类规则（GB 50016 第 5.1.1 条）：
- 住宅：H > 54m → 一类高层；27m < H ≤ 54m → 二类高层；H ≤ 27m → 多层
- 公共建筑：H > 50m → 一类高层；24m < H ≤ 50m → 二类高层；H ≤ 24m → 多层

/no_think"""


def extract_params(
    description: str,
    llm_url: str = LLM_URL,
    model_id: str = LLM_MODEL_ID,
) -> dict[str, Any]:
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"项目描述：{description}"},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(f"{llm_url}/v1/chat/completions", json=payload, timeout=60)
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("```")).strip()

    return json.loads(raw)


@click.command()
@click.option("--description", "-d", required=True, help="项目自由文本描述。")
@click.option("--llm-url", default=LLM_URL, show_default=True)
@click.option("--llm-model-id", default=LLM_MODEL_ID, show_default=True)
@click.option("--output", "output_path", default=None, help="结果写入 JSON 文件。")
def main(description: str, llm_url: str, llm_model_id: str, output_path: str | None) -> None:
    """从自由文本描述提取结构化建筑参数（Qwen3-8B）。"""
    console.print("[dim]提取项目参数...[/dim]")
    try:
        params = extract_params(description, llm_url, llm_model_id)
    except requests.RequestException as e:
        console.print(f"[red]LLM 调用失败：{e}[/red]")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]返回了非法 JSON：{e}[/red]")
        raise SystemExit(1)

    output = json.dumps(params, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(output, encoding="utf-8")
        console.print(f"[green]✓ 参数已写入 {output_path}[/green]")
    else:
        console.print(output)


if __name__ == "__main__":
    main()
