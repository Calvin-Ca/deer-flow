"""生成层 —— 造价规范检索结果 → 结构化带引用回答（被 /norm/qa 使用）。

承旧 ``qa/generation.py``（防火规范问答）的硬约束：**强制引用条文号、无依据拒答、不编造**；
但适配造价规范——知识层 chunk **无 is_mandatory 字段**（强条 2026-06-12 已降级为 modal 表征、不落
检索响应），故本生成器**不做强制/推荐二分**，改为：忠实引用条文，仅当条文正文自带"本条为强制性
条文"等字样时照原文标注。LLM 调用复用 ``common.llm.call_qwen3``（裸 Qwen3-8B JSON 直出）。

输入 clauses 即知识服务 ``/search`` 返回的 ``clauses[]``（字段：node_path / standard_id / content /
references_to / page / _source）；``_source == "ref_expand"`` 为引用图扩展拉来的关联条文。
"""
from __future__ import annotations

from typing import Any

from common.llm import call_qwen3

SYSTEM_PROMPT = """\
你是一名专业的建设工程造价（工程量计算 / 清单计价）规范查询助手，严格基于所提供的规范条文回答用户问题。

**核心约束（必须遵守）**：
1. 每个事实陈述必须引用具体条文号（如"第 3.1.2 条"），若所给条文中无依据，明确说明"所提供条文未涉及"，绝不编造内容或条文号。
2. 忠实引用条文原意，不臆测计算口径；当条文正文自带"本条为强制性条文"等字样时，照原文标注其强制性，不自行判定。
3. 用户问题涉及的工程/计量口径超出所给规范适用范围（如问安装却只给了房建计量规范）时，明确告知，并在 out_of_scope_warnings 中说明。
4. 区分"计量规范（工程量计算规则）"与"计价规范（清单编制 / 综合单价构成）"——不同规范的条文不混引。
5. 回答面向造价从业者（造价员 / 预算 / 审核），可用专业术语但需准确；末尾免责：本回答仅供参考，不替代专业造价审核。

**输出要求**：返回合法的 JSON 对象，不输出任何 JSON 以外的文字。
"""


def build_user_message(query: str, clauses: list[dict]) -> str:
    """检索条文 + 用户问题 → 拼装 user message（按检索召回 / 引用扩展分组）。

    参数：
        query —— 用户自然语言问题。
        clauses —— 知识服务 /search 返回的条文列表（含 _source / node_path / standard_id / content）。
    返回：
        拼好的 user message 字符串（末尾附 /no_think 禁 Qwen3 thinking，避免干扰 JSON 输出）。
    """
    retrieved = [c for c in clauses if c.get("_source") != "ref_expand"]
    ref_expanded = [c for c in clauses if c.get("_source") == "ref_expand"]

    def clause_block(c: dict) -> list[str]:
        return [
            f"--- 条文 {c.get('node_path', '?')} ---",
            f"规范：{c.get('standard_id', '未知')}",
            f"正文：{(c.get('content') or '').strip()}",
            "",
        ]

    lines: list[str] = []
    lines.append(f"用户问题：{query}")
    lines.append("")
    lines.append(f"从造价规范库检索到 {len(clauses)} 条相关条文。请严格基于这些条文回答，不得引用未给出的条文。")
    lines.append("")

    if retrieved:
        lines.append(
            f"【A. 检索召回条文 {len(retrieved)} 条】——直接或语义相关地回答了用户问题，应填入 cited_clauses。"
        )
        lines.append("")
        for c in retrieved:
            lines.extend(clause_block(c))

    if ref_expanded:
        lines.append(
            f"【B. 引用扩展条文 {len(ref_expanded)} 条】——被 A 类条文引用而自动拉取的关联条文，作背景参考。"
        )
        lines.append("")
        for c in ref_expanded:
            lines.extend(clause_block(c))

    lines.append("---")
    lines.append("请严格基于以上条文回答问题，输出合法 JSON，不输出任何 JSON 以外的文字：")
    lines.append("")
    lines.append("""\
{
  "answer": "面向造价从业者的自然语言回答。必须在正文中引用条文号。条文若自带强制性字样则照原文标注。末尾附：本回答仅供参考，不替代专业造价审核。",
  "cited_clauses": [
    {
      "clause": "仅填条文编号，如 3.1.2，不加"条"前缀",
      "standard": "规范编号（取条文的 standard_id）",
      "text": "相关原文（保持完整语义）",
      "relevance": "direct（直接回答问题）或 contextual（背景参考）"
    }
  ],
  "uncertain_aspects": ["需要专业人员核实的方面（如计量口径歧义），若无则空数组"],
  "out_of_scope_warnings": ["用户问题超出所给规范适用范围的提示，若无则空数组"]
}""")
    lines.append("")
    lines.append("/no_think")

    return "\n".join(lines)


WEB_SYSTEM_PROMPT = """\
你是建设工程造价领域的联网检索结果总结助手。给你的是**联网抓取的网页正文**（非本系统权威规范库），\
请严格基于这些网页文本回答用户问题。

**核心约束（必须遵守）**：
1. 只使用所给网页文本中出现的内容；文本没有的条文号、数值、结论一概不得输出（不编造）。
2. 每个事实陈述标注来源序号（如"[来源1]"），与所给来源列表对应。
3. 网页内容与问题无关或不足以回答时，如实说明"检索到的网页未能回答该问题"。
4. 这是**降级检索结果**，正文里不要冒充权威规范条文口径；不确定处列入 uncertain_aspects。
5. 末尾免责：本回答仅供参考，不替代专业造价审核。

**输出要求**：返回合法的 JSON 对象，不输出任何 JSON 以外的文字。
"""


def answer_web(
    query: str,
    sources: list,
    llm_url: str,
    model_id: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """联网兜底总结（FR-K07 web 降级变体）：网页正文 → 带来源序号的总结回答。

    参数：
        query —— 用户问题；sources —— ``web_fallback.WebSource`` 列表（url/title/text）；
        llm_url/model_id/temperature/max_tokens —— LLM 配置。
    返回：``{answer, uncertain_aspects, out_of_scope_warnings}``（web_citations 与硬标注头由
        ``web_fallback`` 在代码层组装，不经 LLM）；LLM 异常上抛由调用方吸收降级。
    """
    lines: list[str] = [f"用户问题：{query}", "", "联网检索到的网页正文（降级来源）："]
    for i, s in enumerate(sources, 1):
        if not getattr(s, "text", ""):
            continue
        lines += [f"--- 来源{i}：{getattr(s, 'title', '')}（{getattr(s, 'url', '')}）---",
                  s.text, ""]
    lines += [
        "---",
        "请严格基于以上网页文本回答问题，输出合法 JSON：",
        '{\n  "answer": "回答正文，事实陈述标注[来源N]，末尾附免责声明",\n'
        '  "uncertain_aspects": ["需人工核实的方面，若无则空数组"],\n'
        '  "out_of_scope_warnings": ["网页内容与问题口径不符的提示，若无则空数组"]\n}',
        "",
        "/no_think",
    ]
    return call_qwen3(WEB_SYSTEM_PROMPT, "\n".join(lines), llm_url, model_id, temperature, max_tokens)


def answer(
    query: str,
    clauses: list[dict],
    llm_url: str,
    model_id: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """造价规范检索结果 → 结构化带引用回答（/norm/qa 的生成步骤）。

    参数：
        query —— 用户问题；clauses —— /search 返回的条文；llm_url/model_id —— Qwen3 vLLM 配置；
        temperature/max_tokens —— 采样参数。
    返回：
        结构化回答 dict（answer / cited_clauses / uncertain_aspects / out_of_scope_warnings）；
        LLM 非 2xx 经 requests.HTTPError、非法 JSON 经 json.JSONDecodeError 上抛。
    """
    user_msg = build_user_message(query, clauses)
    return call_qwen3(SYSTEM_PROMPT, user_msg, llm_url, model_id, temperature, max_tokens)
