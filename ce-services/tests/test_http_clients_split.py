from __future__ import annotations

from unittest.mock import Mock

import requests

from common import cost_client, knowledge_client


def _mock_response(payload: dict | list | None = None, *, status_code: int = 200):
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_knowledge_client_search_hits_rag_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _mock_response({"query": json["query"], "evidence": [], "meta": {}})

    monkeypatch.setattr(requests, "post", fake_post)
    resp = knowledge_client.search("现浇柱怎么计量", standard="gb50854-2024", base_url="http://rag")

    assert captured["url"] == "http://rag/search/clause"
    assert captured["json"]["standard"] == "gb50854-2024"
    assert resp["query"] == "现浇柱怎么计量"


def test_knowledge_client_expand_hits_new_rag_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _mock_response({"evidence": []})

    monkeypatch.setattr(requests, "post", fake_post)
    knowledge_client.expand(["4.1.2"], standard="gb50854-2024", base_url="http://rag")

    assert captured["url"] == "http://rag/expand/clauses"
    assert captured["json"] == {"node_paths": ["4.1.2"], "standard": "gb50854-2024"}


def test_cost_client_bill_match_hits_rag_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _mock_response({"count": 0, "candidates": []})

    monkeypatch.setattr(requests, "post", fake_post)
    cost_client.bill_match("C30现浇柱", "2024", base_url="http://rag")

    assert captured["url"] == "http://rag/search/bill-match"
    assert captured["json"]["description"] == "C30现浇柱"
    assert captured["json"]["spec"] == "2024"


def test_cost_client_price_compose_hits_db_endpoint(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        return _mock_response({"quotas": []})

    monkeypatch.setattr(requests, "get", fake_get)
    cost_client.price_compose("深圳", "010101001", "2024", base_url="http://db")

    assert captured["url"] == "http://db/price/compose/%E6%B7%B1%E5%9C%B3/010101001"
    assert captured["params"]["spec"] == "2024"


def test_cost_client_bill_get_uses_new_db_path(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        return _mock_response({"code": "010101001"})

    monkeypatch.setattr(requests, "get", fake_get)
    resp = cost_client.bill_get("010101001", "2024", base_url="http://db")

    assert captured["url"] == "http://db/bill/010101001"
    assert captured["params"] == {"spec": "2024"}
    assert resp["code"] == "010101001"


def test_cost_client_bill_get_keeps_404_as_none(monkeypatch):
    resp = _mock_response(None, status_code=404)

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: resp)
    assert cost_client.bill_get("010101001", "2024", base_url="http://db") is None
