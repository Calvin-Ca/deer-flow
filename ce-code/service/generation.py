"""生成层 —— 检索结果 → 结构化回答（被检索服务 /qa 使用）。

从 ``scripts/06_generate.py`` 收敛而来，prompt 与调用逻辑逐字保持不变，只把
CLI / rich 展示留在脚本侧。强制引用、强条/推荐区分、无依据拒答等硬约束都在
``SYSTEM_PROMPT`` 与 ``build_user_message`` 里——这正是"生成留 server 端、不下放
自由 agent"的原因：输出契约必须确定可复现。
"""
from __future__ import annotations

import json
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
你是一名专业的建筑规范查询助手，严格基于所提供的规范条款回答用户问题。

**核心约束（必须遵守）**：
1. 每个事实陈述必须引用具体条款号（如"第5.3.4条"），若规范中无依据，明确说明"本规范未涉及"，绝不编造内容
2. 强制性条款（含"必须""严禁""不应""不得"）和推荐性条款（含"宜""可"）必须显式区分，不得合并陈述
3. 用户场景超出当前规范适用范围时，需明确告知，并在 out_of_scope_warnings 中说明
4. 回答面向通用用户（设计师、施工人员、公众），避免过度使用专业缩写
5. 末尾免责：所有回答仅供参考，不替代专业审查

**输出要求**：返回合法的 JSON 对象，不输出任何 JSON 以外的文字。
"""


def build_user_message(query: str, clauses: list[dict]) -> str:
    # 按来源分组：检索召回 vs 引用图扩展
    retrieved = [c for c in clauses if c.get("_source") != "ref_expand"]
    ref_expanded = [c for c in clauses if c.get("_source") == "ref_expand"]
    n_mandatory = sum(1 for c in clauses if c.get("is_mandatory"))

    def clause_block(c: dict) -> list[str]:
        # is_mandatory 显式写入，要求模型原样复制，不自行判断
        mandatory_flag = "true【强制性，必须/严禁/不应/不得】" if c.get("is_mandatory") else "false【推荐性，宜/可】"
        return [
            f"--- 条款 {c['clause_path']} | is_mandatory={mandatory_flag} ---",
            f"规范：{c.get('standard_id', '未知')}",
            f"正文：{c.get('content', '').strip()}",
            "",
        ]

    lines: list[str] = []
    lines.append(f"用户问题：{query}")
    lines.append("")
    lines.append(
        f"从规范库检索到 {len(clauses)} 条相关条款（强制性 {n_mandatory} 条）。"
        f"每条的 is_mandatory 值已明确标注，请在输出 JSON 中原样复制，不要自行判断。"
    )
    lines.append("")

    if retrieved:
        lines.append(
            f"【A. 检索召回条款 {len(retrieved)} 条】"
            f"——这些条款直接或语义相关地回答了用户问题，应填入 applicable_clauses。"
        )
        lines.append("")
        for c in retrieved:
            lines.extend(clause_block(c))

    if ref_expanded:
        lines.append(
            f"【B. 引用扩展条款 {len(ref_expanded)} 条】"
            f"——这些条款是被 A 类条款所引用而自动拉取的关联条款，应填入 referenced_clauses。"
        )
        lines.append("")
        for c in ref_expanded:
            lines.extend(clause_block(c))

    lines.append("---")
    lines.append(
        "请严格基于以上条款回答问题，输出合法 JSON，不输出任何 JSON 以外的文字："
    )
    lines.append("")
    lines.append("""\
{
  "answer": "面向通用用户的自然语言回答。必须在正文中引用条款号。强制性条款（is_mandatory=true）明确标注为"强制要求"，推荐性条款（is_mandatory=false）标注为"推荐"。末尾附：本回答仅供参考，不替代专业审查。",
  "applicable_clauses": [
    {
      "clause": "仅填条款编号，如 4.4.1，不加"条款"前缀",
      "standard": "规范编号",
      "text": "相关原文（保持完整语义）",
      "is_mandatory": true或false（从A类条款的 is_mandatory 标注中原样复制）,
      "relevance": "direct（直接回答问题）或 contextual（背景参考）"
    }
  ],
  "referenced_clauses": [
    {
      "clause": "仅填条款编号，如 4.4.1，不加"条款"前缀",
      "standard": "规范编号",
      "text": "原文关键句",
      "is_mandatory": true或false（从B类条款的 is_mandatory 标注中原样复制）
    }
  ],
  "uncertain_aspects": ["需要专业人员核实的方面，若无则空数组"],
  "out_of_scope_warnings": ["超出本规范适用范围的提示，若无则空数组"]
}""")
    lines.append("")
    lines.append("/no_think")  # 禁用 Qwen3 thinking，避免干扰 JSON 输出

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# vLLM 调用
# ---------------------------------------------------------------------------

def call_qwen3(
    system_prompt: str,
    user_message: str,
    llm_url: str,
    model_id: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    resp = requests.post(
        f"{llm_url}/v1/chat/completions",
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()

    raw_content = resp.json()["choices"][0]["message"]["content"].strip()

    # vLLM 偶尔在 JSON 外包一层 markdown 代码块，去掉它
    if raw_content.startswith("```"):
        lines = raw_content.splitlines()
        raw_content = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    return json.loads(raw_content)


def answer(
    query: str,
    clauses: list[dict],
    llm_url: str,
    model_id: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """检索结果 → 结构化回答（检索服务 /qa 的生成步骤）。"""
    user_msg = build_user_message(query, clauses)
    return call_qwen3(SYSTEM_PROMPT, user_msg, llm_url, model_id, temperature, max_tokens)
