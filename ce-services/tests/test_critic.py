#!/usr/bin/env python3
"""Critic Agent v0 回归（M3 管线④）：零 LLM stub 注入，双模式（pytest / __main__）。

覆盖：① 三类质疑正常产出（含 item_index 透传）② 引用幻觉作废 ③ 类型越界作废
④ 越界 item_index 保留质疑但去指向 ⑤ 空输入/LLM 失败 → need_review（复核未执行≠绿灯）
⑥ 无质疑=空 findings 且 status=ok（不硬凑）⑦ 超上限截断出声。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost.critic import MAX_FINDINGS, review_extraction  # noqa: E402

_TEXT = ("基础采用C30现浇钢筋混凝土独立基础，混凝土用量约120m³。"
         "框架柱为C35现浇钢筋混凝土矩形柱。屋面SBS改性沥青防水卷材两道。")
_ITEMS = [
    {"feature": "C30现浇钢筋混凝土独立基础", "quantity": 120.0},
    {"feature": "矩形柱"},  # 特征不全（缺 C35/现浇）——喂给 Critic 的靶子
]


def test_three_finding_types_pass():
    def stub(s, u):
        return {"findings": [
            {"type": "missing_item", "detail": "屋面防水未列项",
             "source_text": "屋面SBS改性沥青防水卷材两道"},
            {"type": "weak_feature", "item_index": 1, "detail": "矩形柱缺强度等级",
             "source_text": "框架柱为C35现浇钢筋混凝土矩形柱"},
            {"type": "quantity_doubt", "item_index": 0, "detail": "量与原文一致仅示例",
             "source_text": "混凝土用量约120m³"},
        ]}
    env = review_extraction(_TEXT, _ITEMS, llm_fn=stub)
    assert env["status"] == "ok" and env["step"] == "critic_review"
    fs = env["result"]["findings"]
    assert [f["type"] for f in fs] == ["missing_item", "weak_feature", "quantity_doubt"]
    assert fs[1]["item_index"] == 1 and "item_index" not in fs[0]


def test_hallucinated_source_dropped():
    def stub(s, u):
        return {"findings": [
            {"type": "missing_item", "detail": "真", "source_text": "屋面SBS改性沥青防水卷材两道"},
            {"type": "missing_item", "detail": "幻觉", "source_text": "地下室外墙防水"},  # 原文没有
        ]}
    env = review_extraction(_TEXT, _ITEMS, llm_fn=stub)
    assert len(env["result"]["findings"]) == 1
    assert "作废" in env["note"]


def test_invalid_type_dropped():
    def stub(s, u):
        return {"findings": [{"type": "price_doubt", "detail": "自创类别",
                              "source_text": "混凝土用量约120m³"}]}
    env = review_extraction(_TEXT, _ITEMS, llm_fn=stub)
    assert env["result"]["findings"] == [] and "作废" in env["note"]


def test_out_of_range_index_kept_without_pointer():
    def stub(s, u):
        return {"findings": [{"type": "weak_feature", "item_index": 9, "detail": "索引越界",
                              "source_text": "框架柱为C35现浇钢筋混凝土矩形柱"}]}
    env = review_extraction(_TEXT, _ITEMS, llm_fn=stub)
    fs = env["result"]["findings"]
    assert len(fs) == 1 and "item_index" not in fs[0]  # 质疑保留、指向剥离


def test_false_missing_item_dropped_by_code():
    """假漏项代码判（07-05 实测：prompt 铁律治不住）：构件已在草表却被判漏项 → 作废；
    真漏项（原文独有内容）不受误杀。"""
    text = "外墙MU10砖墙350m²。内隔墙为加气混凝土砌块墙200厚。屋面SBS改性沥青防水卷材两道。"
    items = [{"feature": "MU10砖墙"}, {"feature": "加气混凝土砌块墙200厚"}]

    def stub(s, u):
        return {"findings": [
            {"type": "missing_item", "detail": "假漏项：砌块墙其实在草表",
             "source_text": "内隔墙为加气混凝土砌块墙200厚"},
            {"type": "missing_item", "detail": "真漏项：屋面防水",
             "source_text": "屋面SBS改性沥青防水卷材两道"},
        ]}
    env = review_extraction(text, items, llm_fn=stub)
    fs = env["result"]["findings"]
    assert len(fs) == 1 and "屋面" in fs[0]["source_text"]  # 假漏项砍、真漏项留
    assert "作废" in env["note"]


def test_empty_input_and_llm_failure_need_review():
    env1 = review_extraction("", _ITEMS, llm_fn=lambda s, u: {"findings": []})
    assert env1["status"] == "need_review" and "未执行" in env1["note"]

    def boom(s, u):
        raise ValueError("bad json")
    env2 = review_extraction(_TEXT, _ITEMS, llm_fn=boom)
    assert env2["status"] == "need_review" and env2["result"]["findings"] == []


def test_no_findings_is_ok_not_need_review():
    env = review_extraction(_TEXT, _ITEMS, llm_fn=lambda s, u: {"findings": []})
    assert env["status"] == "ok" and env["result"]["findings"] == []  # 复核执行过且无质疑


def test_over_limit_truncated_with_note():
    def stub(s, u):
        return {"findings": [{"type": "missing_item", "detail": f"q{i}",
                              "source_text": "混凝土用量约120m³"} for i in range(MAX_FINDINGS + 3)]}
    env = review_extraction(_TEXT, _ITEMS, llm_fn=stub)
    assert len(env["result"]["findings"]) == MAX_FINDINGS and "截断" in env["note"]


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    failed = 0
    for _name in sorted(k for k in dir() if k.startswith("test_")):
        try:
            globals()[_name]()
            print(f"✓ {_name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {_name}  {type(exc).__name__}: {exc}")
    print(f"\nCritic v0 回归：{'全绿' if not failed else f'{failed} 败'}")
    sys.exit(1 if failed else 0)
