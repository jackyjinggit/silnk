"""格式模板：模板自检、自由格式跳过检查、自由格式修复不动版式。"""

import fitz

import doc_format


def _make_pdf(path, pages=2):
    doc = fitz.open()
    try:
        for index in range(pages):
            page = doc.new_page(width=595.28, height=841.89)
            page.insert_text((80, 100), f"Section {index + 1}", fontsize=12)
            page.insert_text((80, 130), "Body text for format checking." * 3,
                             fontsize=11)
        doc.save(path)
    finally:
        doc.close()


def test_all_builtin_templates_are_well_formed():
    for key, tpl in doc_format.BUILTIN_TEMPLATES.items():
        assert tpl["id"] == key
        assert tpl["name"]
        assert set(tpl["margins_mm"]) == {"top", "bottom", "left", "right"}
    # 磁盘上的 templates/*.json 会合并进 TEMPLATES，内置的必须在里面
    assert set(doc_format.BUILTIN_TEMPLATES) <= set(doc_format.TEMPLATES)


def test_free_format_skips_every_check(tmp_path):
    pdf = tmp_path / "a.pdf"
    _make_pdf(str(pdf), pages=2)
    result = doc_format.analyze(str(pdf), "free_format")
    assert result["free_format"] is True
    assert result["issues"] == []
    assert result["score"] == 100
    assert result["pages"] == 2


def test_free_format_fix_keeps_layout(tmp_path):
    src = tmp_path / "a.pdf"
    _make_pdf(str(src), pages=2)
    out = tmp_path / "out.pdf"
    # 即使调用方传了 fix_margins=True，模板没有页边距标准时也不缩放
    stats = doc_format.fix(str(src), str(out), {
        "remove_blank": True, "fix_margins": True,
        "add_page_numbers": False, "insert_cover": False,
        "duplex": False,
    }, template_name="free_format")
    assert stats["margin_fixed"] == 0
    assert stats["page_numbers_added"] == 0
    assert stats["new_pages"] == 2
    assert stats["removed_blank"] == 0


def test_unknown_template_falls_back_to_thesis():
    assert doc_format.get_template("no_such_template")["id"] == "general_thesis"


def test_new_templates_are_selectable():
    for key in ("free_format", "gb_thesis", "lab_report", "resume",
                "official_doc"):
        assert doc_format.get_template(key)["id"] == key
