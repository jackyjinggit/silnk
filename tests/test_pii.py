"""隐私检查：四类 PII 的识别与一键打码。"""

import fitz

import pii


def test_find_spans_detects_four_types():
    text = ("身份证号：31011520010101001X 手机号：138 0013 8000 "
            "银行卡号：6222 0000 0000 0001 "
            "住址：上海市浦东新区示例路 100 号 1 单元 101")
    kinds = [kind for _, _, kind in pii._find_spans(text)]
    assert kinds == ["身份证号", "手机号", "银行卡号", "住址"]


def test_phone_variants_are_detected():
    for text in ("13800138000", "138 0013 8000", "138-0013-8000"):
        kinds = [kind for _, _, kind in pii._find_spans(text)]
        assert kinds == ["手机号"]


def test_bank_number_requires_card_pattern():
    # Luhn 通过的测试卡号
    assert [k for _, _, k in pii._find_spans("4111111111111111")] == ["银行卡号"]
    # 16 位纯数字但不是卡 BIN 开头，不当成银行卡
    assert pii._find_spans("1234567890123456") == []


def test_scan_and_redact_removes_all_matches(tmp_path):
    src = tmp_path / "pii.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "身份证号 31011520010101001X", fontsize=12)
    page.insert_text((72, 130), "手机号 138 0013 8000", fontsize=12)
    page.insert_text((72, 160), "银行卡号 6222 0000 0000 0001", fontsize=12)
    doc.save(str(src))
    doc.close()

    result = pii.scan(str(src))
    assert result["total"] == 3
    assert set(result["by_type"]) == {"身份证号", "手机号", "银行卡号"}

    dst = tmp_path / "pii-redacted.pdf"
    out = pii.redact(str(src), str(dst))
    assert out["redacted"] == 3
    assert pii.scan(str(dst))["total"] == 0
