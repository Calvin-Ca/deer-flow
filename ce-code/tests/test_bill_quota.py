"""bill_quota 纯函数单测（清单→定额 APPLIES 名称匹配 + 国标版本隔离）。"""
from cost.bill_quota import _compose_capable_versions, match


def test_match_emits_bill_spec_version():
    # 每条映射带清单所属国标版本（版本隔离：同 9 位码跨版本不共用映射）
    bills = [{"code": "010401002", "name": "实心砖墙", "spec_version": "GB/T 50854-2024"}]
    quotas = [{"quota_code": "010001-3", "name": "实心砖墙 1/2砖", "doc_id": "SZ-SJG171"}]
    rows = match(bills, quotas)
    assert len(rows) == 1
    assert rows[0]["bill_code"] == "010401002"
    assert rows[0]["bill_spec_version"] == "GB/T 50854-2024"
    assert rows[0]["source"] == "auto_name_exact"


def test_compose_capable_includes_2013():
    # 定额与清单国标版本解耦，2013 清单同样套现行 SJG 定额 → 应纳入可组价集合
    caps = _compose_capable_versions()
    assert "GB/T 50854-2024" in caps
    assert "GB/T 50856-2024" in caps
    assert "GB/T 50854-2013" in caps
