"""quota_scheme 纯函数单测（清单命中定额 → 可替代方案启发式分组）。"""
import pytest

from cost.quota_scheme import _quota_prefix, group_quota_schemes, refine_schemes_llm


def test_quota_prefix_strips_suffix():
    assert _quota_prefix("010001-3") == "010001"
    assert _quota_prefix("010001") == "010001"
    assert _quota_prefix("") == ""


def test_single_prefix_multi_quota_splits_into_schemes():
    # 同 6 位前缀、多条且无其他工序 = 同表并排可替代做法 → 逐条成方案候选（各 1 子目）
    quotas = [
        {"quota_code": "010001-1", "name": "干混砂浆砌筑", "confidence": 0.9, "source": "auto_name", "base_price": 100},
        {"quota_code": "010001-2", "name": "湿拌砂浆砌筑", "confidence": 0.7, "source": "semantic_llm", "base_price": 110},
    ]
    schemes = group_quota_schemes(quotas)
    assert len(schemes) == 2
    assert {scheme["scheme_id"] for scheme in schemes} == {"010001-1", "010001-2"}
    assert schemes[0]["strategy"] == "heuristic_prefix_combo"
    # 按 score 降序：干混 0.9 在前
    assert schemes[0]["score"] == 0.9
    assert schemes[0]["base_price_subtotal"] == 100.0


def test_multi_subitem_scheme_combines_alt_and_fixed_workprocedures():
    # 可替代工序（浇筑 010001 干混/湿拌）× 必选构成工序（模板 010502） → 每套方案是多子目组合
    quotas = [
        {"quota_code": "010001-1", "name": "干混浇筑", "confidence": 0.9, "base_price": 100},
        {"quota_code": "010001-2", "name": "湿拌浇筑", "confidence": 0.7, "base_price": 110},
        {"quota_code": "010502-1", "name": "组合钢模板", "confidence": 0.8, "base_price": 50},
    ]
    schemes = group_quota_schemes(quotas)
    assert len(schemes) == 2  # 浇筑 2 做法 × 模板 1 = 2 套
    for scheme in schemes:
        assert len(scheme["quota_codes"]) == 2  # 每套 = 浇筑1条 + 模板1条（多子目）
        assert "010502-1" in scheme["quota_codes"]  # 必选构成每套都含
        assert scheme["strategy"] == "heuristic_prefix_combo"
    # 短板评分：方案含 0.8 模板 → 干混套 min(0.9,0.8)=0.8 排在湿拌套 min(0.7,0.8)=0.7 之前
    assert schemes[0]["score"] == 0.8
    assert schemes[0]["base_price_subtotal"] == 150.0
    assert schemes[1]["score"] == 0.7


def test_cross_prefix_falls_back_to_single_scheme():
    # 跨前缀（构成子目混合，替代关系不可靠）→ 保守返回单方案不拆
    quotas = [
        {"quota_code": "010001-1", "name": "浇筑", "confidence": 0.9, "base_price": 100},
        {"quota_code": "010502-1", "name": "模板", "confidence": 0.8, "base_price": 50},
    ]
    schemes = group_quota_schemes(quotas)
    assert len(schemes) == 1
    assert schemes[0]["scheme_id"] == "__full__"
    assert schemes[0]["strategy"] == "single_full"
    assert schemes[0]["base_price_subtotal"] == 150.0
    assert schemes[0]["quota_codes"] == ["010001-1", "010502-1"]


def test_single_quota_is_single_scheme():
    quotas = [{"quota_code": "010001-1", "name": "浇筑", "confidence": 0.9}]
    schemes = group_quota_schemes(quotas)
    assert len(schemes) == 1
    assert schemes[0]["strategy"] == "single_full"


def test_empty_quotas_returns_empty():
    assert group_quota_schemes([]) == []


def test_base_price_subtotal_all_none():
    quotas = [
        {"quota_code": "010001-1", "confidence": 0.9},
        {"quota_code": "010001-2", "confidence": 0.7},
    ]
    schemes = group_quota_schemes(quotas)
    assert all(scheme["base_price_subtotal"] is None for scheme in schemes)


def test_refine_schemes_llm_is_reserved():
    with pytest.raises(NotImplementedError):
        refine_schemes_llm([{"quota_code": "010001-1"}])
