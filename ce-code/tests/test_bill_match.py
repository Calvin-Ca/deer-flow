"""bill_match / bill_index 纯函数单测（不连 Milvus/PG/嵌入服务）。

覆盖：嵌入文本拼装（缺项省略、特征/章节并入）+ Milvus 命中整形（字段抽取 + score 取整）。
真链路（PG 建库 + 向量召回）在服务器跑，本地仅验纯逻辑（torch cu121 本地不可用）。
"""
from cost.bill_index import bill_embed_text
from cost.bill_match import _shape_hits


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
