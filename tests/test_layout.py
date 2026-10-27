"""多页合一排版：页数与纸张尺寸。"""

import fitz

import layout


def _make_pdf(path, pages):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()


def test_nup_4_pages_per_sheet(tmp_path):
    src = tmp_path / "src.pdf"
    dst = tmp_path / "out.pdf"
    _make_pdf(src, 5)
    stats = layout.nup(str(src), str(dst), 4)
    assert stats == {"per_sheet": 4, "original_pages": 5, "new_pages": 2}
    doc = fitz.open(str(dst))
    try:
        assert doc.page_count == 2
        assert round(doc.load_page(0).rect.width) == 595
        assert round(doc.load_page(0).rect.height) == 842
    finally:
        doc.close()


def test_nup_2_and_6(tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src, 7)
    for per_sheet, expected in ((2, 4), (6, 2)):
        dst = tmp_path / f"out{per_sheet}.pdf"
        stats = layout.nup(str(src), str(dst), per_sheet)
        assert stats["new_pages"] == expected


def _page_count(path):
    doc = fitz.open(str(path))
    try:
        return doc.page_count
    finally:
        doc.close()


def test_layout_endpoint_can_switch_and_restore(tmp_path, monkeypatch):
    """接口层：切档位始终从原始备份重排，1 页/张能恢复原样。"""
    import jwt
    from routers.user import LayoutRequest, apply_layout

    monkeypatch.chdir(tmp_path)
    token = jwt.create_token()
    folder = jwt.decode_token(token)["token"]
    converted = tmp_path / "uploads" / folder / "converted"
    converted.mkdir(parents=True)
    source = converted / "test.pdf"
    _make_pdf(source, 5)

    first = apply_layout(token, "test.pdf", LayoutRequest(per_sheet=4))
    assert first.message == "success"
    assert first.data["new_pages"] == 2
    assert _page_count(source) == 2

    # 改成 2 页/张时从原始 5 页重排，而不是把 2 页再合并
    second = apply_layout(token, "test.pdf", LayoutRequest(per_sheet=2))
    assert second.data["new_pages"] == 3

    restored = apply_layout(token, "test.pdf", LayoutRequest(per_sheet=1))
    assert restored.data["new_pages"] == 5
    assert _page_count(source) == 5
