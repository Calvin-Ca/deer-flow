#!/usr/bin/env python3
"""列清单解析器 + 批量点火回归（M2 v0）：零 LLM 零服务（stub 注入），双模式（pytest / __main__）。

覆盖：
  抽取器 extract_components ——
    ① 正常两件（溯源过校验、quantity 透传）② 引用幻觉作废（source_text 不在原文）
    ③ 编造工程量作废字段（负数/非数值）④ 空输入 ⑤ LLM 异常降级 need_review
    ⑥ 输出缺 items 降级 ⑦ 超 MAX_ITEMS 截断带注记
  编排接线 _ignite_listing ——
    ⑧ 多件点火（features 透传含 Q + marker + listing 摘要）⑨ 0 件引导 need_input
    ⑩ 点火失败降级返回草稿
  session 归一 ——
    ⑪ _norm_feature 透传 quantity、丢弃非法 Q（不依赖 langgraph，纯函数……经由 stub 间接验，
       见 ⑧ 的 features 断言；直接单测归 graph 测试文件——本文件不 import cost.session）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost.listing import MAX_ITEMS, extract_components  # noqa: E402
from routing.orchestrator import _ignite_listing  # noqa: E402
from routing.prerouter import route  # noqa: E402

_TEXT = ("基础工程：C30现浇钢筋混凝土独立基础，混凝土量120m³。"
         "主体：C35现浇矩形柱600×600，HRB400钢筋。墙体采用MU10标准砖240厚实心砖墙M5水泥砂浆砌筑。")


def _stub_ok(system: str, user: str) -> dict:
    return {"items": [
        {"feature": "C30现浇钢筋混凝土独立基础", "source_text": "C30现浇钢筋混凝土独立基础",
         "quantity": 120, "unit": "m3"},
        {"feature": "C35现浇矩形柱600×600 HRB400钢筋", "source_text": "C35现浇矩形柱600×600"},
    ]}


def test_extract_ok_with_quantity():
    env = extract_components(_TEXT, llm_fn=_stub_ok)
    assert env["status"] == "ok" and env["step"] == "extract_components"
    items = env["result"]["items"]
    assert len(items) == 2
    assert items[0]["quantity"] == 120.0 and items[0]["unit"] == "m3"
    assert "quantity" not in items[1]  # 原文无量→不编
    assert env["provenance"]["source_type"] == "user_input"


def test_extract_hallucinated_source_dropped():
    def stub(s, u):
        return {"items": [
            {"feature": "真件", "source_text": "C30现浇钢筋混凝土独立基础"},
            {"feature": "幻觉件", "source_text": "C50预应力空心板"},  # 原文没有
        ]}
    env = extract_components(_TEXT, llm_fn=stub)
    assert env["status"] == "ok" and len(env["result"]["items"]) == 1
    assert "1 条" in env["note"] and "作废" in env["note"]  # 作废出声不静默


def test_extract_fake_quantity_dropped():
    def stub(s, u):
        return {"items": [{"feature": "基础", "source_text": "独立基础", "quantity": -5},
                          {"feature": "柱", "source_text": "矩形柱", "quantity": "很多"}]}
    env = extract_components(_TEXT, llm_fn=stub)
    assert all("quantity" not in it for it in env["result"]["items"])  # 不编量


def test_extract_empty_input():
    env = extract_components("  ", llm_fn=_stub_ok)
    assert env["status"] == "need_review" and env["result"]["items"] == []


def test_extract_llm_failure_degrades():
    def boom(s, u):
        raise ValueError("bad json")
    env = extract_components(_TEXT, llm_fn=boom)
    assert env["status"] == "need_review" and env["result"]["items"] == []
    assert "失败" in env["note"]


def test_extract_missing_items_key():
    env = extract_components(_TEXT, llm_fn=lambda s, u: {"foo": 1})
    assert env["status"] == "need_review"


def test_extract_over_limit_noted():
    def stub(s, u):
        return {"items": [{"feature": f"件{i}", "source_text": "矩形柱"} for i in range(MAX_ITEMS + 5)]}
    env = extract_components(_TEXT, llm_fn=stub)
    assert len(env["result"]["items"]) == MAX_ITEMS
    assert "分批" in env["note"]


# ── 编排接线（stub extract + stub session.start，本地零 langgraph）──

_DECISION = route("根据设计说明列清单：" + _TEXT)


def test_ignite_listing_multi_item():
    assert _DECISION.capability == "cost" and _DECISION.matched["listing"]
    started: dict = {}

    def stub_start(**kw):
        started.update(kw)
        return {"task_id": "t-listing", "status": "awaiting_input"}

    out = _ignite_listing("q", _DECISION,
                          extract_fn=lambda t: extract_components(_TEXT, llm_fn=_stub_ok),
                          start_fn=stub_start)
    assert out["mode"] == "hitl" and out["task_id"] == "t-listing"
    assert "cost-hitl" in out["marker"]
    assert out["listing"]["count"] == 2 and len(out["listing"]["preview"]) == 2
    feats = started["features"]
    assert feats[0] == {"feature": "C30现浇钢筋混凝土独立基础", "quantity": 120.0}  # Q 随件透传
    assert feats[1] == {"feature": "C35现浇矩形柱600×600 HRB400钢筋"}
    assert started["region"]  # 口径透传


def test_ignite_listing_zero_items_guides():
    out = _ignite_listing("帮我列一下清单", _DECISION,
                          extract_fn=lambda t: extract_components("", llm_fn=_stub_ok),
                          start_fn=lambda **kw: (_ for _ in ()).throw(AssertionError("不应点火")))
    assert out["mode"] == "single" and out["result"]["status"] == "need_input"
    assert "设计说明" in out["result"]["note"]


def test_ignite_listing_start_failure_degrades():
    def bad_start(**kw):
        raise ValueError("graph down")
    out = _ignite_listing("q", _DECISION,
                          extract_fn=lambda t: extract_components(_TEXT, llm_fn=_stub_ok),
                          start_fn=bad_start)
    assert out["result"]["status"] == "degraded"
    assert out["result"]["extraction"]["result"]["items"]  # 草稿仍在，不丢用户成果


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
    print(f"\n列清单 v0 回归：{'全绿' if not failed else f'{failed} 败'}")
    sys.exit(1 if failed else 0)
