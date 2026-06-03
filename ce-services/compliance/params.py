"""合规编排 · 参数提取 —— 自由文本 → 结构化建筑参数（被 orchestration 使用）。

prompt 与逻辑逐字不变（原 ce-code/service/params.py 平移而来）。唯一改动：LLM 地址
从 ``retrieval.config.DEFAULTS`` 改取任务层 ``common.config``（任务层不依赖 retrieval）。
"""
from __future__ import annotations

import json
from typing import Any

import requests

from common.config import LLM_MODEL_ID, LLM_URL

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
