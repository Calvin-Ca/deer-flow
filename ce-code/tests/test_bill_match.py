"""bill_match / bill_index 纯函数单测（不连 Milvus/PG/嵌入服务）。

覆盖：嵌入文本拼装（缺项省略、特征/章节并入）+ Milvus 命中整形（字段抽取 + score 取整）。
真链路（PG 建库 + 向量召回）在服务器跑，本地仅验纯逻辑（torch cu121 本地不可用）。
"""
from cost.bill_index import bill_embed_text
from cost.bill_match import _rerank_text, _shape_hits


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
