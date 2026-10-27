"""定时打印（预约出纸）：入库、到点放行、超时作废、用户取消、时间校验。"""

import os
import time
from datetime import datetime, timedelta

import automatic_tasks
import db
from print_queue import print_queue_singleton
from routers import pay as pay_router

FMT = "%Y-%m-%d %H:%M:%S"


def _at(minutes=30):
    return (datetime.now() + timedelta(minutes=minutes)).strftime(FMT)


def _job(folder="S1", filename="a.pdf", **extra):
    job = {"folder": folder, "filename": filename,
           "print_copies": 1, "print_range_start": 1, "print_range_end": 1,
           "print_side": "one-sided", "status": "scheduled"}
    job.update(extra)
    return job


def test_add_job_keeps_scheduled_at(temp_db):
    at = _at()
    jid = temp_db.add_job(_job(scheduled_at=at, user_id=7))
    assert temp_db.get_job_by_id(jid)["scheduled_at"] == at


def test_immediate_job_has_no_scheduled_at(temp_db):
    jid = temp_db.add_job(_job(status="pending"))
    assert temp_db.get_job_by_id(jid)["scheduled_at"] is None


def test_list_due_only_returns_reached(temp_db):
    due = temp_db.add_job(_job(filename="due.pdf", scheduled_at=_at(-1)))
    temp_db.add_job(_job(filename="later.pdf", scheduled_at=_at(30)))
    temp_db.add_job(_job(filename="now.pdf", status="pending"))
    assert [j["id"] for j in temp_db.list_due_scheduled_jobs()] == [due]


def test_release_enqueues_due_job(temp_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("pending_files/S1", exist_ok=True)
    with open("pending_files/S1/a.pdf", "wb") as f:
        f.write(b"%PDF-1.4")
    jid = temp_db.add_job(_job(scheduled_at=_at(-1)))
    queue = print_queue_singleton().data
    queue.clear()
    try:
        assert automatic_tasks.release_scheduled_jobs() == [jid]
        assert [j["id"] for j in queue] == [jid]
        assert queue[0]["status"] == "pending"
        assert temp_db.get_job_by_id(jid)["status"] == "pending"
    finally:
        queue.clear()


def test_release_marks_failed_when_file_missing(temp_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jid = temp_db.add_job(_job(filename="gone.pdf", scheduled_at=_at(-1)))
    queue = print_queue_singleton().data
    queue.clear()
    try:
        assert automatic_tasks.release_scheduled_jobs() == []
        assert temp_db.get_job_by_id(jid)["status"] == "failed"
    finally:
        queue.clear()


def test_expire_stale_scheduled(temp_db):
    stale = temp_db.add_job(_job(filename="old.pdf", scheduled_at=_at(-13 * 60)))
    fresh = temp_db.add_job(_job(filename="new.pdf", scheduled_at=_at(-60)))
    immediate = temp_db.add_job(_job(filename="now.pdf", status="pending"))
    assert automatic_tasks.expire_stale_scheduled() == 1
    assert temp_db.get_job_by_id(stale)["status"] == "expired"
    assert temp_db.get_job_by_id(fresh)["status"] == "scheduled"
    assert temp_db.get_job_by_id(immediate)["status"] == "pending"


def test_expire_removes_released_job_from_memory_queue(temp_db, tmp_path, monkeypatch):
    """到点放行后在内存队列里滞留、又被 12 小时作废的预约单，必须从队列剔除。"""
    monkeypatch.chdir(tmp_path)
    os.makedirs("pending_files/S1", exist_ok=True)
    with open("pending_files/S1/a.pdf", "wb") as f:
        f.write(b"%PDF-1.4")
    # 一张 13 小时前的预约单：先被放行进内存队列（status=pending），再被作废
    jid = temp_db.add_job(_job(scheduled_at=_at(-13 * 60)))
    queue = print_queue_singleton().data
    queue.clear()
    try:
        assert automatic_tasks.release_scheduled_jobs() == [jid]
        assert [j["id"] for j in queue] == [jid]
        assert automatic_tasks.expire_stale_scheduled() == 1
        assert temp_db.get_job_by_id(jid)["status"] == "expired"
        assert queue == []
    finally:
        queue.clear()


def test_cancel_scheduled_job(temp_db):
    jid = temp_db.add_job(_job(scheduled_at=_at(), user_id=7))
    ok, msg, job = temp_db.cancel_scheduled_job(jid, user_id=7)
    assert ok and job["status"] == "canceled"
    assert temp_db.get_job_by_id(jid)["status"] == "canceled"


def test_cancel_anonymous_requires_matching_folder(temp_db):
    """匿名会话下单（无 uid）时，只有下单会话目录一致才能取消。"""
    jid = temp_db.add_job(_job(folder="S9", scheduled_at=_at()))
    # 没有 uid 且目录不匹配 → 拒绝，防止匿名会话越权取消他人预约单
    ok, msg, _ = temp_db.cancel_scheduled_job(jid, folder="OTHER")
    assert not ok
    assert temp_db.get_job_by_id(jid)["status"] == "scheduled"
    # 目录与会话一致 → 允许本会话取消自己的预约单
    ok, msg, job = temp_db.cancel_scheduled_job(jid, folder="S9")
    assert ok and job["status"] == "canceled"


def test_cancel_logged_in_cannot_cancel_anonymous(temp_db):
    """登录用户拿到别人的匿名预约单号也取消不了（目录不符）。"""
    jid = temp_db.add_job(_job(folder="S8", scheduled_at=_at()))
    ok, msg, _ = temp_db.cancel_scheduled_job(jid, user_id=7, folder="S7")
    assert not ok
    assert temp_db.get_job_by_id(jid)["status"] == "scheduled"


def test_cancel_rejects_other_user(temp_db):
    jid = temp_db.add_job(_job(scheduled_at=_at(), user_id=7))
    ok, msg, _ = temp_db.cancel_scheduled_job(jid, user_id=8)
    assert not ok
    assert temp_db.get_job_by_id(jid)["status"] == "scheduled"


def test_cancel_rejects_started_job(temp_db):
    jid = temp_db.add_job(_job(status="pending", user_id=7))
    ok, msg, _ = temp_db.cancel_scheduled_job(jid, user_id=7)
    assert not ok
    assert "无法取消" in msg


def test_parse_scheduled_at_empty_is_immediate():
    assert pay_router._parse_scheduled_at(None) == (None, None, None)
    assert pay_router._parse_scheduled_at("") == (None, None, None)


def test_parse_scheduled_at_accepts_valid_forms():
    text, value, err = pay_router._parse_scheduled_at(_at(30).rsplit(":", 1)[0])
    assert err is None and text.endswith(":00") and value is not None
    # unix 秒（前端用 datetime-local，秒位固定 00）
    text, value, err = pay_router._parse_scheduled_at(time.time() + 1800)
    assert err is None and value is not None


def test_parse_scheduled_at_rejects_bad_input():
    _, _, err = pay_router._parse_scheduled_at("2020-01-01 10:00")
    assert "1 分钟" in err
    _, _, err = pay_router._parse_scheduled_at(_at(8 * 24 * 60))
    assert "7 天" in err
    _, _, err = pay_router._parse_scheduled_at("not-a-time")
    assert err
