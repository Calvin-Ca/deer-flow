"""bill_match / bill_index 纯函数单测（不连 Milvus/PG/嵌入服务）。

覆盖：嵌入文本拼装（缺项省略、特征/章节并入）+ Milvus 命中整形（字段抽取 + score 取整）。
真链路（PG 建库 + 向量召回）在服务器跑，本地仅验纯逻辑（torch cu121 本地不可用）。
"""
from cost.bill_index import bill_embed_text, cast_type
from cost.bill_match import (
    _prefab_penalty, _prefix_filter, _rerank_text, _shape_hits, _structural_reorder, _type_penalty,
)


def test_cast_type_prefab():
    assert cast_type("表 E.9 预制混凝土柱（编号：010509）", "1.m³2.根") == "预制"


def test_cast_type_cast_in_place_untagged():
    # 现浇柱不强加标记（返回空），避免给非混凝土项贴错
    assert cast_type("表 E.2 现浇混凝土柱（编号：010502）", "m³") == ""
    assert cast_type(None, "m²") == ""


def test_cast_type_minerU_spaced_caption():
    # MinerU 中文插空格：'预 制' 须折叠空白后仍判为预制（notebooks E8 bug）
    assert cast_type("表 E. ， 预 制混 凝 土柱 （编 号 ：0105)", "1.m32.根") == "预制"
    assert cast_type("续表 装 配 式", "m³") == "装配"


def test_prefab_penalty_demotes_unrequested_prefab():
    assert _prefab_penalty("柱；混凝土强度等级C40", {"cast_type": "预制"}) == 1
    assert _prefab_penalty("预制柱安装", {"cast_type": "预制"}) == 0     # query 明示预制 → 不罚
    assert _prefab_penalty("柱；C40", {"cast_type": ""}) == 0            # 候选非预制 → 不罚


def test_structural_reorder_demotes_prefab_keeps_castinplace():
    # 同名「矩形柱」现浇 vs 预制，dense 把预制排前 → 重排应翻转
    cands = [
        {"code": "010509001", "name": "矩形柱", "cast_type": "预制"},
        {"code": "010502001", "name": "矩形柱", "cast_type": ""},
    ]
    out = _structural_reorder("柱；混凝土强度等级C40", cands)
    assert out[0]["code"] == "010502001"
    assert out[1]["code"] == "010509001"


def test_resolve_spec_known():
    import config
    assert config.resolve_spec("2024")["bill_collection"] == "cost_bill_spec_kb"
    assert config.resolve_spec("2013")["bill_collection"] == "cost_bill_spec_kb_2013"
    assert config.resolve_spec("2024")["supports_compose"] is True
    assert config.resolve_spec("2013")["supports_compose"] is False
    assert config.resolve_spec(" 2024 ")["bill_collection"] == "cost_bill_spec_kb"  # 去空白
    assert config.resolve_spec("2024")["bill_doc_ids"] == ["GB-50854", "GB-50856"]
    assert config.resolve_spec("2013")["bill_doc_ids"] == ["GB-50854-2013"]


def test_resolve_spec_required_and_unknown():
    import config
    import pytest
    with pytest.raises(ValueError):
        config.resolve_spec("")        # 必填、无默认
    with pytest.raises(ValueError):
        config.resolve_spec("2099")    # 未知版本


def test_prefix_filter_single():
    assert _prefix_filter(["01"]) == 'code like "01%"'


def test_prefix_filter_multi():
    assert _prefix_filter(["01", "03"]) == 'code like "01%" or code like "03%"'


def test_prefix_filter_none_or_empty():
    assert _prefix_filter(None) == ""
    assert _prefix_filter([]) == ""
    assert _prefix_filter([" ", ""]) == ""


def test_embed_text_full():
    """名称 + 特征 + 章节齐全时按「。」拼，特征以「/」连。"""
    t = bill_embed_text("实心砖墙", ["砖品种规格", "墙体类型"], "附录D 砌筑工程")
    assert t == "实心砖墙。特征:砖品种规格/墙体类型。附录D 砌筑工程"


def test_embed_text_no_feature():
    """无特征项时省略特征段，不留空「特征:」。"""
    assert bill_embed_text("挖单独土方", [], "附录A 土石方工程") == "挖单独土方。附录A 土石方工程"


def test_embed_text_only_name():
    """特征/章节均缺（None）时只剩名称，无悬挂分隔符。"""
    assert bill_embed_text("某清单项", None, None) == "某清单项"


def test_embed_text_strips_blank_feature():
    """特征列表内空串/空白被过滤，不污染拼装。"""
    assert bill_embed_text("梁", [" ", "混凝土强度等级", ""], "") == "梁。特征:混凝土强度等级"


def test_shape_hits():
    """Milvus 命中整形：抽 entity 字段 + distance→score（4 位）。"""
    hits = [
        {"entity": {"code": "010401002", "name": "实心砖墙", "unit": "m³",
                    "feature": "砖品种", "chapter": "附录D", "doc_id": "GB-50854",
                    "spec_version": "GB/T 50854-2024"}, "distance": 0.876543},
    ]
    out = _shape_hits(hits)
    assert len(out) == 1
    assert out[0]["code"] == "010401002"
    assert out[0]["score"] == 0.8765
    assert out[0]["spec_version"] == "GB/T 50854-2024"


def test_shape_hits_empty():
    """空命中返回空列表。"""
    assert _shape_hits([]) == []


def test_rerank_text_from_candidate():
    """候选(feature 为 '/'-串)还原为与建库同构的配对文本。"""
    cand = {"name": "矩形柱", "feature": "图代号/混凝土强度等级", "chapter": "附录E 混凝土工程"}
    assert _rerank_text(cand) == "矩形柱。特征:图代号/混凝土强度等级。附录E 混凝土工程"


def test_rerank_text_empty_feature():
    """feature 空串时省略特征段，与 bill_embed_text 行为一致。"""
    cand = {"name": "平整场地", "feature": "", "chapter": "附录A"}
    assert _rerank_text(cand) == "平整场地。附录A"


# ── 结构约束（类型对齐重排）────────────────────────────────────────────────────
def test_type_penalty_template_demoted():
    """查询要本体(无「模板」)，候选「圈梁模板」带模板标记→罚 1；本体「圈梁」罚 0。"""
    assert _type_penalty("C25现浇混凝土圈梁", "圈梁模板") == 1
    assert _type_penalty("C25现浇混凝土圈梁", "圈梁") == 0


def test_type_penalty_steel_material_word_not_intent():
    """「钢筋混凝土」是材料词、非要钢筋项：本体查询里它不触发钢筋罚，柱钢筋候选仍被罚。"""
    # 查询「现浇混凝土矩形柱」无钢筋意图 → 柱钢筋候选罚 1
    assert _type_penalty("C30现浇混凝土矩形柱", "现浇混凝土柱钢筋") == 1
    # 查询含「钢筋混凝土」材料词 → 归一后无钢筋意图，柱钢筋候选仍罚 1（不被材料词误免罚）
    assert _type_penalty("C30现浇钢筋混凝土独立基础", "现浇混凝土基础钢筋") == 1
    # 候选本体「钢筋混凝土管」归一后无「钢筋」→ 不误罚
    assert _type_penalty("钢筋混凝土管", "钢筋混凝土管") == 0


def test_type_penalty_steel_intent_kept():
    """查询明确要钢筋项（非材料词）→ 柱钢筋候选不罚（类型对齐）。"""
    assert _type_penalty("现浇混凝土柱内HRB400钢筋制作安装", "现浇混凝土柱钢筋") == 0


def test_structural_reorder_stable():
    """稳定重排：罚分低者靠前，同罚分保持原序。本体反超模板，部位同罚分不动。"""
    cands = [
        {"name": "圈梁模板"},          # 罚 1（被压后）
        {"name": "圈梁"},              # 罚 0（应升首位）
        {"name": "楼地面卷材防水"},     # 罚 0（同罚分，保持原相对序）
    ]
    out = _structural_reorder("C25现浇混凝土圈梁", cands)
    assert [c["name"] for c in out] == ["圈梁", "楼地面卷材防水", "圈梁模板"]
    assert out[0]["type_penalty"] == 0 and out[-1]["type_penalty"] == 1


# ── xlsx → gold 转换（tools.build_match_gold，2013 版隔离）────────────────────────
from tools.build_match_gold import build_query, clean_feature, code9, rows_to_gold


def test_code9_strips_project_suffix():
    """12 位 xlsx 码取前 9 位规范码（去 3 位项目自编）。"""
    assert code9("010202007001") == "010202007"
    assert code9("010202007") == "010202007"


def test_clean_feature_collapses():
    """多行项目特征折叠为单行紧凑文本。"""
    assert clean_feature("(1)地层情况:综合\n(2)类型:锚杆  ") == "(1)地层情况:综合 (2)类型:锚杆"


def test_build_query_excludes_name():
    """查询=构件名+特征，不含清单 NAME（避免循环）。"""
    q = build_query("锚索", "(1)类型:基坑底抗拔锚杆")
    assert q == "锚索；(1)类型:基坑底抗拔锚杆" and "锚杆(锚索)" not in q


def test_rows_to_gold_dedup_and_coverage():
    """同(码,特征)去重；不在库的码进 uncovered 不进 gold。"""
    rows = [
        {"CODE": "010202007001", "NAME": "锚杆(锚索)", "FEATURE": "类型:抗拔", "COMP_NAME": "锚索"},
        {"CODE": "010202007002", "NAME": "锚杆(锚索)", "FEATURE": "类型:抗拔", "COMP_NAME": "锚索"},  # 同(码9,特征)→去重
        {"CODE": "999999999001", "NAME": "未知项", "FEATURE": "x", "COMP_NAME": "y"},               # 不在库→uncovered
    ]
    gold, uncovered = rows_to_gold(rows, valid_codes={"010202007"})
    assert len(gold) == 1 and gold[0]["gold"] == ["010202007"]
    assert len(uncovered) == 1 and uncovered[0]["code9"] == "999999999"


# ── 评测 harness 纯指标（tools.eval_bill）────────────────────────────────────────
from tools.eval_bill import aggregate, first_gold_rank


def test_first_gold_rank_hit():
    """首个命中金标的 1-based 秩。"""
    assert first_gold_rank({"B"}, ["A", "B", "C"]) == 2
    assert first_gold_rank({"A"}, ["A", "B"]) == 1


def test_first_gold_rank_multi_gold():
    """多金标取最靠前命中。"""
    assert first_gold_rank({"C", "B"}, ["A", "B", "C"]) == 2


def test_first_gold_rank_miss():
    """top_k 内无金标→None。"""
    assert first_gold_rank({"Z"}, ["A", "B"]) is None


def test_aggregate_metrics():
    """Top-1/Top-3/Recall/MRR/平均命中秩 计算。ranks=[1,2,None,4]，k=10。"""
    m = aggregate([1, 2, None, 4], 10)
    assert m["n"] == 4
    assert m["top1"] == 0.25           # 1 个 rank==1
    assert m["top3"] == 0.5            # rank<=3: {1,2}
    assert m["recall_at_k"] == 0.75    # 3/4 命中(<=10)
    assert abs(m["mrr"] - (1 + 0.5 + 0.25) / 4) < 1e-9
    assert abs(m["mean_hit_rank"] - (1 + 2 + 4) / 3) < 1e-9


def test_aggregate_empty():
    """空 ranks 不除零，mean_hit_rank=None。"""
    m = aggregate([], 10)
    assert m["n"] == 0 and m["mean_hit_rank"] is None
