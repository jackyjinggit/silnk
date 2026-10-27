"""共享资料库：待审入库、agent 结果、管理员复审与公开文件流转。"""

import os

import fitz
import pytest

import library


def _make_pdf(path, pages=2):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def lib_dirs(tmp_path, monkeypatch):
    """把待审/公开目录指到临时目录，并跳过异步 agent 线程。"""
    pending = tmp_path / "pending"
    public = tmp_path / "public"
    pending.mkdir()
    public.mkdir()
    monkeypatch.setattr(library, "PENDING_DIR", str(pending))
    monkeypatch.setattr(library, "PUBLIC_DIR", str(public))
    monkeypatch.setattr(library, "_run_agent_review", lambda doc_id: None)
    return pending, public


def _capture(temp_db, tmp_path, pages=3, title="高数复习.pdf", uploader_uid=None):
    src = tmp_path / "order.pdf"
    _make_pdf(src, pages)
    return library.capture_pending(
        str(src), title, "论文", uploader_uid=uploader_uid,
        tenant_id="default", order_no="WX_TEST", index=0)


def test_capture_creates_pending_doc(temp_db, lib_dirs, tmp_path):
    doc_id = _capture(temp_db, tmp_path, pages=3)
    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "pending_agent"
    assert doc["pages"] == 3
    assert doc["category"] == "论文"
    assert doc["title"] == "高数复习"
    assert doc["uploader_name"] == "匿名用户"
    assert os.path.isfile(doc["pending_path"])


def test_agent_pass_moves_to_admin_queue(temp_db, lib_dirs, tmp_path):
    doc_id = _capture(temp_db, tmp_path)
    temp_db.set_library_agent_result(doc_id, "pass", "前 5 页正常", "test-model")
    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "pending_admin"
    assert doc["agent_verdict"] == "pass"


def test_agent_block_rejects(temp_db, lib_dirs, tmp_path):
    doc_id = _capture(temp_db, tmp_path)
    temp_db.set_library_agent_result(doc_id, "block", "明确违规", "test-model")
    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "rejected"
    assert doc["agent_verdict"] == "block"


def test_agent_unavailable_goes_to_manual_review(temp_db, lib_dirs, tmp_path):
    """AI 未接入时必须转人工，不能算通过。"""
    doc_id = _capture(temp_db, tmp_path)
    temp_db.set_library_agent_result(doc_id, "unavailable", "缺少 GLM_API_KEY")
    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "pending_admin"
    assert doc["agent_verdict"] == "unavailable"


def test_publish_moves_file_and_marks_published(temp_db, lib_dirs, tmp_path):
    pending_dir, public_dir = lib_dirs
    doc_id = _capture(temp_db, tmp_path)
    temp_db.set_library_agent_result(doc_id, "pass", "ok")
    ok, message = library.publish(doc_id, reviewer="tester")
    assert ok and message == "success"

    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "published"
    assert doc["filename"] == f"library-{doc_id}.pdf"
    assert os.path.isfile(doc["published_path"])
    assert os.path.dirname(doc["published_path"]) == str(public_dir)
    assert os.listdir(pending_dir) == []
    assert temp_db.count_library_docs("published") == 1
    assert temp_db.list_library_docs(status="published", q="高数")[0]["id"] == doc_id


def test_publish_requires_admin_stage(temp_db, lib_dirs, tmp_path):
    doc_id = _capture(temp_db, tmp_path)  # 仍在 agent 初审
    ok, _msg = library.publish(doc_id)
    assert not ok
    assert temp_db.get_library_doc(doc_id)["status"] == "pending_agent"


def test_reject_deletes_pending_file(temp_db, lib_dirs, tmp_path):
    pending_dir, _public_dir = lib_dirs
    doc_id = _capture(temp_db, tmp_path)
    temp_db.set_library_agent_result(doc_id, "warn", "检测到手机号")
    ok, _msg = library.reject(doc_id, "含个人信息，不适合公开")
    assert ok
    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "rejected"
    assert doc["admin_note"].startswith("含个人信息")
    assert os.listdir(pending_dir) == []


def test_download_count(temp_db, lib_dirs, tmp_path):
    doc_id = _capture(temp_db, tmp_path)
    temp_db.bump_library_download(doc_id)
    temp_db.bump_library_download(doc_id)
    assert temp_db.get_library_doc(doc_id)["downloads"] == 2


def test_admin_upload_publishes_directly(temp_db, lib_dirs, tmp_path):
    """管理员后台上传视为已审核，直接公开。"""
    src = tmp_path / "admin.pdf"
    _make_pdf(src, 2)
    doc_id, message = library.publish_external(
        str(src), "管理员上传测试.pdf", "文学", uploader_name="管理员上传")
    assert message == "success"
    doc = temp_db.get_library_doc(doc_id)
    assert doc["status"] == "published"
    assert doc["title"] == "管理员上传测试"
    assert doc["uploader_name"] == "管理员上传"
    assert doc["category"] == "文学"
    assert doc["agent_verdict"] == "pass"


def test_print_count_drives_contribution_discount(temp_db, lib_dirs, tmp_path):
    """打印量只统计已公开资料；1 次 3%，5 次 5%。"""
    doc_id = _capture(temp_db, tmp_path, uploader_uid=7)
    temp_db.add_library_print(doc_id, 20)

    # 未公开前不计入贡献统计
    assert temp_db.library_contributor_stats(7)["print_count"] == 0
    assert library.contribution_of(7)["contribution_rate"] == 0

    temp_db.set_library_agent_result(doc_id, "pass", "ok")
    library.publish(doc_id, reviewer="tester")

    stats = temp_db.library_contributor_stats(7)
    assert stats["print_count"] == 1
    assert stats["print_pages"] == 20
    info = library.contribution_of(7)
    assert info["contribution_rate"] == 0.03
    assert info["next_need"] == 5

    for _ in range(4):
        temp_db.add_library_print(doc_id, 10)
    info = library.contribution_of(7)
    assert info["print_count"] == 5
    assert info["print_pages"] == 60
    assert info["contribution_rate"] == 0.05
    assert info["next_need"] == 20
    assert info["next_rate"] == 0.08


def test_category_add_delete_and_clean(temp_db, lib_dirs):
    """分类可自定义：新增后可作为分类使用，删除后归入「其他」。"""
    ok, msg = library.add_category("竞赛")
    assert ok and msg == "success"
    assert "竞赛" in library.list_categories()
    assert library.clean_category("竞赛") == "竞赛"

    ok, msg = library.add_category("竞赛")
    assert not ok and msg == "分类已存在"

    assert library.delete_category("竞赛") == (True, "success")
    assert library.clean_category("竞赛") == "其他"


def test_category_in_use_cannot_be_deleted(temp_db, lib_dirs, tmp_path):
    """仍有资料使用的分类不能删，「其他」永远保留。"""
    _capture(temp_db, tmp_path)
    ok, msg = library.delete_category("论文")
    assert not ok and "不能删除" in msg
    assert "论文" in library.list_categories()

    ok, msg = library.delete_category("其他")
    assert not ok and msg.startswith("「其他」")
    assert "其他" in library.list_categories()
