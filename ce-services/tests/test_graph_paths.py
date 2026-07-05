#!/usr/bin/env python3
"""HITL 组价图全路径 stub 回归（M1 收尾）：零 LLM 零服务跑通所有暂停/恢复/回环/终态路径。

做法：monkeypatch 三个原语挂点（``provenance.list_match`` / ``provenance.from_price_compose`` /
``clarify.extract_missing_features``，均为 graph 节点内的模块属性查找，patch 模块属性即生效），
用 ``MemorySaver`` 内存 checkpointer 建图——不写文件、不调 LLM、不打 :8100。

每个暂停闸的 payload 同时过 ``contracts.validate_interrupt``：把契约测试从「手写样例」升级为
「真图产出」，补上 test_contracts 里 review/session 只能用镜像样例的缺口。

双模式（约定同 tools/test_backlog.py）：服务器 ``uv run pytest tests/ -q``；本地无 langgraph
自动 skip（``python tests/test_graph_paths.py`` 打印 skip 退 0）。

覆盖路径：
  ① 全供给直通：仅末尾 review 恒停 → approve → done（含税总价确定性可验算）
  ② 低置信停编码闸 → manual_override 钉码 + override/audit 记录
  ③ 缺特征澄清回环：feature_gate 停 → 补特征 → 回 list_match 重匹配（stub 计数证实重调）
  ④ 缺定额映射：全空放弃 → 单构件 no_pricing → blocked（不虚构总价）
  ⑤ 缺定额半填：重问一次 → 再全空 → blocked
  ⑥ 缺定额补录齐 → 钉用户基价续算到 review → done
  ⑦ 缺价逐项录入：no_source 材料停闸 → 录价 → user_input 落 provenance
  ⑧ 缺工程量 Q：quantity_gate 停 → 录 Q → 续跑
  ⑨ rewind 时间旅行：done 后回退 list_gate 重停（上游 compute 不重跑，stub 计数证实）
  ⑩ 多构件外层循环：好件+坏件 → done（坏件计 missing 不拖死整单）
  ⑪ session 并发锁：同 task_id 同一把锁、异 task_id 各一把
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    _LANGGRAPH = True
except ImportError:  # 本地无 langgraph：pytest 模块级 skip / __main__ 打印跳过
    _LANGGRAPH = False

if not _LANGGRAPH and __name__ != "__main__":
    import pytest

    pytest.skip("langgraph 未装（本地环境）——graph 回归在服务器跑", allow_module_level=True)

if _LANGGRAPH:
    from cost import clarify, provenance
    from cost.contracts import validate_interrupt
    from cost.graph import build_graph

# ── stub 素材（形状对齐 provenance.list_match / from_price_compose 真实产出）──

GOOD_CODE = "010503002001"
QUOTA = {"子目号": "A1-15", "name": "矩形柱", "labor_cost": 100.0, "material_cost": 200.0,
         "machine_cost": 50.0}
MAT_OK = {"raw": "混凝土", "std": "C30商品混凝土", "unit": "m3", "consumption": 1.0,
          "price": {"value": 480.0, "status": "ok",
                    "provenance": {"source_type": "price_book", "source_ref": "深圳信息价2024-06"}}}
MAT_NO_SOURCE = {"raw": "特种砂浆", "std": "特种砂浆", "unit": "t", "consumption": 0.2,
                 "price": {"value": None, "status": "no_source", "provenance": {}}}
RATES = {"management_fee_rate": 10.0, "profit_rate": 5.0, "risk_rate": 0.0, "fee_base": "labor"}
PARAMS = {"measure_fee": 0.0, "other_fee": 0.0, "fee_levy": 0.0, "tax_rate": 9.0}


def make_env(code=GOOD_CODE, conf=0.9, status="ok", alternatives=()):
    return {"step": "list_match", "status": status,
            "result": {"code": code, "name": "矩形柱", "unit": "m3"},
            "provenance": {"source_type": "spec_clause", "source_ref": "GB50854-2024 附录E",
                           "confidence": conf, "alternatives": list(alternatives)}}


def make_bundle(quotas=(QUOTA,), materials=(MAT_OK,)):
    return {"quota_envelope": {"step": "pick_quota", "status": "ok",
                               "result": {"quotas": [dict(q) for q in quotas]},
                               "provenance": {"source_type": "quota_lib",
                                              "source_ref": "深圳2024定额", "alternatives": []}},
            "materials": [dict(m, price=dict(m["price"])) for m in materials]}


@contextmanager
def patched(list_envs=None, bundle=None, missing=None):
    """monkeypatch 三个原语挂点并计数调用；退出恢复。

    参数：list_envs —— list_match 逐次返回的信封序列（耗尽则重复末个；None=默认高置信）；
      bundle —— from_price_compose 返回；missing —— extract_missing_features 逐次返回序列。
    产出：calls dict（list_match / compose / missing 调用次数，供断言重匹配/不重跑）。
    """
    calls = {"list_match": 0, "compose": 0, "missing": 0}
    envs = list(list_envs or [make_env()])
    missings = list(missing or [[]])
    bundle = bundle if bundle is not None else make_bundle()
    orig = (provenance.list_match, provenance.from_price_compose, clarify.extract_missing_features)

    def _lm(feature, spec, top_k):
        i = min(calls["list_match"], len(envs) - 1)
        calls["list_match"] += 1
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in envs[i].items()}

    def _pc(region, code, spec):
        calls["compose"] += 1
        return make_bundle() if bundle == "fresh" else bundle

    def _mf(feature, hints, url, model):
        i = min(calls["missing"], len(missings) - 1)
        calls["missing"] += 1
        return missings[i]

    provenance.list_match, provenance.from_price_compose = _lm, _pc
    clarify.extract_missing_features = _mf
    try:
        yield calls
    finally:
        provenance.list_match, provenance.from_price_compose, clarify.extract_missing_features = orig


_seq = [0]


def run(initial_over=None, **patch_kw):
    """建独立图（MemorySaver）+ 唯一 thread，invoke 初始态；返回 (graph, config, result, calls 提取器)。"""
    _seq[0] += 1
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": f"t{_seq[0]}"}}
    initial = {"task_id": f"t{_seq[0]}", "region": "深圳", "spec_version": "2024",
               "current_item": 0, "status": "running",
               "items": [{"feature": "C30现浇混凝土矩形柱", "quantity": 2.0}],
               "rates": dict(RATES), "params": dict(PARAMS)}
    initial.update(initial_over or {})
    return graph, config, initial


def gate(result):
    """取暂停闸 payload 并过契约校验（每个暂停点都验 contracts —— 真图产出级契约测试）。"""
    interrupts = result.get("__interrupt__")
    assert interrupts, f"期望暂停但直接跑完：status={result.get('status')}"
    payload = interrupts[0].value
    validate_interrupt(payload)
    return payload


# ── ① 全供给直通：只停末尾 review；approve → done；含税总价可确定性验算 ──
def test_happy_path_review_only():
    with patched() as calls:
        graph, config, initial = run()
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["gate_type"] == "review" and g["node"] == "rollup"
        # 验算：直接费=350，labor 基数管理费 10+利润 5 → 单价 365，×Q2=730，税 9% → 795.7
        assert abs(g["rollup"]["total"] - 795.7) < 0.01, g["rollup"]
        r2 = graph.invoke(Command(resume={"action": "approve"}), config)
        assert r2["status"] == "done"
        assert calls["list_match"] == 1 and calls["compose"] == 1  # LLM/取数各恰一次


# ── ② 低置信停编码闸：manual_override 钉码 + override/audit 落账 ──
def test_low_confidence_pauses_then_override():
    with patched(list_envs=[make_env(conf=0.5)]):
        graph, config, initial = run()
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["gate_type"] == "confirm" and g["node"] == "list_coding"
        assert g["confidence_band"] == "low"  # <τ_low=0.6 低置信分段
        r2 = graph.invoke(Command(resume={"action": "manual_override", "value": "010101001001"}), config)
        g2 = gate(r2)  # 后续照常停 review
        assert g2["gate_type"] == "review"
        state = graph.get_state(config).values
        assert state["items"][0]["code"]["value"] == "010101001001"
        assert any(o.get("node") == "code" for o in state.get("overrides", []))


# ── ③ 缺特征澄清回环：补特征 → 回 list_match 重匹配（重调证实）──
def test_feature_clarify_loop_rematches():
    envs = [make_env(code=None, conf=None, status="need_review"), make_env()]
    with patched(list_envs=envs, missing=[[{"key": "grade", "label": "强度等级", "why": "选码需要"}], []]) as calls:
        graph, config, initial = run()
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["gate_type"] == "input" and g["node"] == "feature"
        r2 = graph.invoke(Command(resume={"grade": "C30"}), config)
        assert calls["list_match"] == 2  # 回环重匹配发生
        g2 = gate(r2)
        assert g2["gate_type"] == "review"  # 第二轮高置信直通到 review
        state = graph.get_state(config).values
        assert "强度等级=C30" in state["items"][0]["feature"]


# ── ④ 缺定额映射全空放弃 → 单构件 no_pricing → blocked，不虚构总价 ──
def test_quota_missing_abandon_blocks():
    with patched(bundle=make_bundle(quotas=())):
        graph, config, initial = run()
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["gate_type"] == "input" and g["node"] == "quota_missing"
        # 全空=放弃。注意：直调 graph 须模拟 session.resume 的空 dict→哨兵归一
        # （langgraph 把空 dict resume 当「未提供恢复值」会同闸重停，session.py 注释坑）
        r2 = graph.invoke(Command(resume={"__resume__": True}), config)
        assert r2["status"] == "blocked"
        assert "无法组价到总价" in r2["rollup"]["blocked_reason"]


# ── ⑤ 缺定额半填 → 重问一次（partial 文案）→ 再全空 → blocked ──
def test_quota_missing_partial_reasks_once():
    with patched(bundle=make_bundle(quotas=())):
        graph, config, initial = run()
        graph.invoke(initial, config)
        r2 = graph.invoke(Command(resume={"labor_cost": 10.0}), config)  # 半填
        g2 = gate(r2)
        assert g2["node"] == "quota_missing" and "补齐" in g2["title"]  # partial 重问（title 带「请补齐…或全空放弃」后缀）
        r3 = graph.invoke(Command(resume={"__resume__": True}), config)  # 空 dict→哨兵（同④）
        assert r3["status"] == "blocked"


# ── ⑥ 缺定额补录齐 → 钉用户基价续算 → review → done ──
def test_quota_missing_manual_basis_continues():
    with patched(bundle=make_bundle(quotas=())):
        graph, config, initial = run()
        graph.invoke(initial, config)
        r2 = graph.invoke(Command(resume={"quota_code": "自定-1", "labor_cost": 100.0,
                                          "material_cost": 200.0, "machine_cost": 50.0}), config)
        g2 = gate(r2)
        assert g2["gate_type"] == "review"
        assert abs(g2["rollup"]["total"] - 795.7) < 0.01  # 与①同基价同费率 → 同总价
        state = graph.get_state(config).values
        assert state["items"][0]["quota"]["provenance"]["source_type"] == "user_input"


# ── ⑦ 缺价逐项录入：no_source 停闸 → 录价 → user_input 落 provenance；命中的不问 ──
def test_price_gate_no_source_asks_only_missing():
    with patched(bundle=make_bundle(materials=(MAT_OK, MAT_NO_SOURCE))):
        graph, config, initial = run()
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["gate_type"] == "input" and g["node"].startswith("price_item:")
        assert g["context"]["std"] == "特种砂浆"  # 只问缺价那条，命中的绝不问（§7）
        r2 = graph.invoke(Command(resume={"value": 3000.0}), config)
        g2 = gate(r2)
        assert g2["gate_type"] == "review"
        mats = graph.get_state(config).values["items"][0]["materials"]
        assert mats[1]["price"]["status"] == "user_input" and mats[1]["price"]["value"] == 3000.0


# ── ⑧ 缺工程量 Q：quantity_gate 停 → 录 Q → 续跑（绝不按 1 计）──
def test_quantity_gate_requires_q():
    with patched():
        graph, config, initial = run({"items": [{"feature": "C30现浇混凝土矩形柱"}]})  # 不预供 Q
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["gate_type"] == "input" and g["node"] == "quantity"
        r2 = graph.invoke(Command(resume={"quantity": 2.0}), config)
        g2 = gate(r2)
        assert abs(g2["rollup"]["total"] - 795.7) < 0.01  # Q=2 与①一致


# ── ⑨ rewind 时间旅行：done 后回退 list_gate 重停；上游 compute 不重跑 ──
def test_rewind_to_list_gate_no_recompute():
    with patched(list_envs=[make_env(conf=0.5)]) as calls:
        graph, config, initial = run()
        graph.invoke(initial, config)                                   # 停 list_gate
        graph.invoke(Command(resume={"action": "approve"}), config)     # 到 review
        graph.invoke(Command(resume={"action": "approve"}), config)     # done
        lm_before = calls["list_match"]
        target = next((s for s in graph.get_state_history(config) if "list_gate" in (s.next or ())), None)
        assert target is not None
        r = graph.invoke(None, config=target.config)                    # 回退重跑该闸
        g = gate(r)
        assert g["gate_type"] == "confirm" and g["node"] == "list_coding"
        assert calls["list_match"] == lm_before  # 上游 LLM compute 有 checkpoint，不重跑（原则 3）


# ── ⑩ 多构件外层循环：好件+坏件 → done；坏件计 missing 不拖死整单 ──
def test_multi_item_bad_item_does_not_block():
    bundles = {"good": make_bundle(), "bad": make_bundle(quotas=())}
    seen = []

    def _pc(region, code, spec):
        seen.append(code)
        return bundles["good"] if len(seen) == 1 else bundles["bad"]

    with patched():
        provenance.from_price_compose = _pc  # 覆盖 patched 的 compose：首件好、次件缺定额
        graph, config, initial = run({"items": [{"feature": "C30矩形柱", "quantity": 2.0},
                                                {"feature": "神秘构件", "quantity": 1.0}]})
        r = graph.invoke(initial, config)
        g = gate(r)
        assert g["node"] == "quota_missing"          # 第二件缺定额停闸
        r2 = graph.invoke(Command(resume={"__resume__": True}), config)  # 放弃第二件（空→哨兵，同④）
        g2 = gate(r2)
        assert g2["gate_type"] == "review"           # 有可算件 → 照常收尾，不 no_pricing
        assert g2["rollup"]["missing_unit_price_items"] == 1
        r3 = graph.invoke(Command(resume={"action": "approve"}), config)
        assert r3["status"] == "done"


# ── ⑪ session 并发锁：同 task 同锁、异 task 异锁（不依赖图执行）──
def test_session_task_lock_identity():
    from cost import session

    a1, a2, b = session._task_lock("task-a"), session._task_lock("task-a"), session._task_lock("task-b")
    assert a1 is a2 and a1 is not b


# ── ⑫ batch_resume 批量续跑（评审表后端）：决策池全命中 → 一次到 done ──
def test_batch_resume_full_pool_to_done():
    from cost import session

    with patched(list_envs=[make_env(conf=0.5)]):  # 双件均低置信 → 各停 list_coding
        res = session.start(features=[{"feature": "C30矩形柱甲", "quantity": 2.0},
                                      {"feature": "C30矩形柱乙", "quantity": 1.0}],
                            spec="2024", rates=dict(RATES))
        task_id = res["task_id"]
        assert res["status"] == "awaiting_input" and res["interrupt"]["node"] == "list_coding"
        out = session.batch_resume(task_id, [
            {"node": "list_coding", "item": 0, "decision": {"action": "approve"}},
            {"node": "list_coding", "item": 1, "decision": {"action": "approve"}},
            {"node": "params", "decision": dict(PARAMS)},
            {"node": "rollup", "decision": {"action": "approve"}},
        ])
        assert out["batch"]["stopped_reason"] == "done", out["batch"]
        assert out["status"] == "done" and out["interrupt"] is None
        assert len(out["batch"]["consumed"]) == 4 and out["batch"]["remaining_decisions"] == 0
        assert out["rollup"]["total"] > 0  # 双件合价 → 有总造价


# ── ⑬ batch_resume 部分池：未给决策的闸停下留人工（评审表「低置信标黄」语义）──
def test_batch_resume_partial_pool_stops_at_unmatched():
    from cost import session

    with patched(list_envs=[make_env(conf=0.5)]):
        # 两件都预供 Q（否则第一件 approve 后先停 quantity 闸，测不到「停在第二件编码闸」）
        res = session.start(features=[{"feature": "C30矩形柱甲", "quantity": 2.0},
                                      {"feature": "C30矩形柱乙", "quantity": 1.0}],
                            spec="2024", rates=dict(RATES))
        out = session.batch_resume(res["task_id"], [
            {"node": "list_coding", "item": 0, "decision": {"action": "approve"}},
            # 刻意不给 item 1 的决策
        ])
        assert out["batch"]["stopped_reason"] == "no_match"
        assert out["status"] == "awaiting_input"
        assert out["interrupt"]["node"] == "list_coding"  # 停在第二件的编码闸
        assert out["batch"]["consumed"] == [{"node": "list_coding", "item": 0}]


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not _LANGGRAPH:
        print("skip：langgraph 未装（本地环境）——graph 回归在服务器跑")
        sys.exit(0)
    failed = 0
    for _name in sorted(k for k in dir() if k.startswith("test_")):
        try:
            globals()[_name]()
            print(f"✓ {_name}")
        except Exception as exc:  # noqa: BLE001 —— 直跑模式逐个报错不中断
            failed += 1
            print(f"✗ {_name}  {type(exc).__name__}: {exc}")
    print(f"\ngraph 全路径回归：{'全绿' if not failed else f'{failed} 败'}")
    sys.exit(1 if failed else 0)
