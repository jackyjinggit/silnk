"""计价核心纯逻辑测试：覆盖率分档、双面按张、空白页/份数口径。"""

import pytest

import pricing

CFG = {"bw": 0.3, "color": 0.8, "bw_duplex": 0.5, "color_duplex": 1.5}


def test_single_page_price_tiers():
    # 阈值来自 pricing：<0.5% 黑白价；0.5%~5% 五折；5%~30% 八折；>30% 全价
    assert pricing.single_page_price(None, 0.3, 0.8) == 0.3
    assert pricing.single_page_price(0.004, 0.3, 0.8) == 0.3
    assert pricing.single_page_price(0.005, 0.3, 0.8) == pytest.approx(0.4)
    assert pricing.single_page_price(0.05, 0.3, 0.8) == pytest.approx(0.4)
    assert pricing.single_page_price(0.051, 0.3, 0.8) == pytest.approx(0.64)
    assert pricing.single_page_price(0.30, 0.3, 0.8) == pytest.approx(0.64)
    assert pricing.single_page_price(0.301, 0.3, 0.8) == pytest.approx(0.8)


def test_duplex_sheet_price():
    # 两面都无彩色 → 黑白双面价
    assert pricing.duplex_sheet_price(None, None, 0.3, 0.8, 0.5, 1.5) == 0.5
    assert pricing.duplex_sheet_price(0.001, 0.001, 0.3, 0.8, 0.5, 1.5) == 0.5
    # 有一面彩色：两面单页价之和 ×（双面彩价 / 2×单面彩价）
    expected = (0.8 + 0.8) * (1.5 / (2 * 0.8))
    assert pricing.duplex_sheet_price(0.9, 0.9, 0.3, 0.8, 0.5, 1.5) == pytest.approx(expected)


def test_price_file_one_sided_bw_with_copies():
    priced = pricing.price_file(None, 1, 4, 2, "one-sided", "bw", True, CFG)
    assert priced["chargeable_count"] == 4
    assert priced["sheets_per_copy"] == 4
    assert priced["amount"] == pytest.approx(0.3 * 4 * 2)
    assert priced["saved_amount"] == 0


def test_price_file_duplex_odd_pages():
    # 3 页双面黑白：1 张双面 0.5 + 最后 1 页按单面 0.3
    priced = pricing.price_file(None, 1, 3, 1, "two-sided-long-edge", "bw", True, CFG)
    assert priced["sheets_per_copy"] == 2
    assert priced["amount"] == pytest.approx(0.8)


def test_price_file_duplex_even_pages_with_copies():
    priced = pricing.price_file(None, 1, 4, 3, "two-sided-long-edge", "bw", True, CFG)
    assert priced["sheets_per_copy"] == 2
    assert priced["amount"] == pytest.approx(0.5 * 2 * 3)


def test_price_file_color_without_pdf_uses_full_color_price():
    # pdf_path 为空时不分析覆盖率，按彩色全价计
    priced = pricing.price_file(None, 1, 2, 1, "one-sided", "color", False, CFG)
    assert priced["amount"] == pytest.approx(0.8 * 2)


def test_price_file_empty_range_returns_none():
    assert pricing.price_file(None, 3, 2, 1, "one-sided", "bw", True, CFG) is None
