from __future__ import annotations

import io
from typing import Any

import openpyxl

from app.ce.io.excel_tools import parse_boq_excel, parse_boq_excel_tool, parse_boq_workbook


def _wb_bytes(rows: list[list[Any]], sheet_title: str = "清单") -> bytes:
    """把二维数据写成 xlsx 字节流，供纯函数测试使用。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_HEADER = ["序号", "项目编码", "项目名称", "项目特征", "计量单位", "工程量", "综合单价", "合价"]


def test_tool_name_matches_config():
    assert parse_boq_excel.name == "parse_boq_excel"
    assert parse_boq_excel_tool is parse_boq_excel


def test_parse_happy_path_with_title_rows_above_header():
    rows = [
        ["某工程 分部分项工程量清单", None, None, None, None, None, None, None],  # 标题行
        _HEADER,
        [None, None, "土石方工程", None, None, None, None, None],  # 分部行（无编码）
        [1, "010101001001", "平整场地", "三类土", "m2", 1200.5, None, None],
        [2, "010502001001", "矩形柱", "C30 现浇", "m3", "1,250.5", 850.0, 10625.0],
    ]
    result = parse_boq_workbook(_wb_bytes(rows))

    assert result["status"] == "ok"
    assert result["sheet"] == "清单"
    assert result["header_row"] == 2  # 1 基，标题行在其上
    assert result["columns"]["code"] == "B"
    assert result["columns"]["quantity"] == "F"
    assert result["row_count"] == 3

    items = result["items"]
    # 分部行：无编码、is_bill=False
    assert items[0]["name"] == "土石方工程"
    assert items[0]["is_bill"] is False
    # 清单行
    assert items[1]["code"] == "010101001001"
    assert items[1]["is_bill"] is True
    assert items[1]["quantity"] == 1200.5
    assert items[1]["row"] == 4  # 1 基原始行号
    # 千分位工程量解析
    assert items[2]["quantity"] == 1250.5
    assert items[2]["unit_price"] == 850.0


def test_code_leading_zeros_preserved_when_stored_as_number():
    # 编码被存成数字时也要还原成不带小数点的字符串（不丢位、不出现 .0）
    rows = [_HEADER, [1, 10105.0, "名称", "特征", "m", 3, None, None]]
    result = parse_boq_workbook(_wb_bytes(rows))
    assert result["items"][0]["code"] == "10105"


def test_missing_header_returns_error_with_preview():
    rows = [["随便", "一些", "文字"], ["和", "数据", "没有表头"]]
    result = parse_boq_workbook(_wb_bytes(rows))
    assert result["status"] == "error"
    assert "表头" in result["error"]
    assert result["preview"]  # 附前几行供澄清


def test_sheet_not_found_returns_error():
    result = parse_boq_workbook(_wb_bytes([_HEADER]), sheet="不存在")
    assert result["status"] == "error"
    assert result["sheet_names"] == ["清单"]


def test_all_empty_quantity_emits_warning():
    rows = [_HEADER, [1, "010101001001", "平整场地", "三类土", "m2", None, None, None]]
    result = parse_boq_workbook(_wb_bytes(rows))
    assert result["status"] == "ok"
    assert any("工程量" in w for w in result["warnings"])


def test_blank_row_between_items_does_not_stop_extraction():
    rows = [
        _HEADER,
        [1, "010101001001", "平整场地", "三类土", "m2", 10, None, None],
        [None, None, None, None, None, None, None, None],  # 单个空行
        [2, "010502001001", "矩形柱", "C30", "m3", 20, None, None],
    ]
    result = parse_boq_workbook(_wb_bytes(rows))
    assert result["row_count"] == 2


class _FakeSandbox:
    def __init__(self, data: bytes):
        self._data = data

    def download_file(self, path: str) -> bytes:
        assert path.startswith("/mnt/user-data/")
        return self._data


def test_tool_wrapper_delegates_to_parser(monkeypatch):
    data = _wb_bytes([_HEADER, [1, "010101001001", "平整场地", "三类土", "m2", 10, None, None]])
    monkeypatch.setattr("app.ce.io.excel_tools.ensure_sandbox_initialized", lambda runtime: _FakeSandbox(data))

    result = parse_boq_excel.func(object(), "/mnt/user-data/uploads/boq.xlsx")
    assert result["status"] == "ok"
    assert result["items"][0]["code"] == "010101001001"


def test_tool_wrapper_reports_missing_file(monkeypatch):
    def _raise(runtime):
        raise FileNotFoundError()

    monkeypatch.setattr("app.ce.io.excel_tools.ensure_sandbox_initialized", _raise)
    result = parse_boq_excel.func(object(), "/mnt/user-data/uploads/none.xlsx")
    assert result["status"] == "error"
    assert "不存在" in result["error"]
