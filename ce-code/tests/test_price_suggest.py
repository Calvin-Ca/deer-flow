"""price_suggest 纯函数单测（信息价缺失料 → 近似料启发式召回打分）。"""
from cost.price_suggest import _norm, ngrams, suggest_prices, suggest_prices_llm

import pytest


def test_norm_strips_spec_noise_and_fullwidth():
    assert _norm("干混砂浆 M7.5") == "干混砂浆m7.5"
    assert _norm("钢筋（HRB400）") == "钢筋hrb400"


def test_ngrams_basic():
    assert ngrams("干混砂浆") == {"干混", "混砂", "砂浆"}


def test_suggest_recommends_near_material_when_substr_misses():
    # "干混砌筑砂浆" 子串查不到，但库里有 "干混砂浆 M10" → 近似召回应命中
    pool = [
        {"name": "干混砂浆 M10", "category": "材料", "price": 460},
        {"name": "商品混凝土 C30", "category": "材料", "price": 520},
    ]
    out = suggest_prices("干混砌筑砂浆", "材料", pool)
    assert len(out) == 1
    assert out[0]["name"] == "干混砂浆 M10"
    assert out[0]["match"] == "heuristic_ngram"
    assert out[0]["score"] > 0.34


def test_suggest_filters_cross_category():
    # 同名相近但类别不同（材料 vs 机械）→ 不跨类推荐
    pool = [{"name": "干混砂浆搅拌机", "category": "机械", "price": 300}]
    assert suggest_prices("干混砂浆", "材料", pool) == []


def test_suggest_below_threshold_returns_empty():
    pool = [{"name": "螺纹钢筋 HRB400", "category": "材料", "price": 3800}]
    assert suggest_prices("商品混凝土", "材料", pool) == []


def test_suggest_sorts_by_coverage_desc_and_topk():
    pool = [
        {"name": "干混砂浆 M10", "category": "材料", "price": 460},
        {"name": "干混抹灰砂浆 M15", "category": "材料", "price": 480},
        {"name": "干混地面砂浆", "category": "材料", "price": 450},
    ]
    out = suggest_prices("干混砂浆", "材料", pool, top_k=2)
    assert len(out) == 2
    assert out[0]["score"] >= out[1]["score"]


def test_suggest_llm_is_reserved():
    with pytest.raises(NotImplementedError):
        suggest_prices_llm({"name": "商砼"}, [{"name": "商品混凝土"}])
