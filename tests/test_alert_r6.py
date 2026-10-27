"""R6 出纸异常：心跳在线 + printing 超 5 分钟 + 无出纸事件 → 告警；事件到达后自动关窗。"""

from datetime import datetime, timedelta

import alerts
import db


DEV = "r6-dev"


def _setup_device(monkeypatch, heartbeat_seconds_ago=0):
    monkeypatch.setattr(alerts.notify, "push_alert", lambda alert: None)
    db.register_device(DEV, name="R6 测试机", paper_capacity=250)
    db.update_heartbeat(DEV, status="online", paper_left=200)
    if heartbeat_seconds_ago:
        _set_heartbeat(DEV, heartbeat_seconds_ago)


def _set_heartbeat(device_id, seconds_ago):
    ts = (datetime.now() - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn = db._connect()
    try:
        conn.execute("UPDATE devices SET last_heartbeat=? WHERE device_id=?",
                     (ts, device_id))
        conn.commit()
    finally:
        conn.close()


def _printing_job(name="r6.pdf", seconds_in_printing=360):
    job_id = db.add_job({"folder": "f", "filename": name, "status": "pending",
                         "out_trade_no": "T" + name}, printer_id=DEV)
    db.set_job_status("f", name, "printing")
    ts = (datetime.now() - timedelta(seconds=seconds_in_printing)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn = db._connect()
    try:
        conn.execute("UPDATE print_jobs SET status_at=? WHERE id=?", (ts, job_id))
        conn.commit()
    finally:
        conn.close()
    return job_id


def _r6_message():
    for alert in db.list_alerts():
        if alert.get("rule") == "R6" and alert.get("device_id") == DEV:
            return alert.get("message") or ""
    return ""


def test_r6_triggers_when_printing_without_eject(temp_db, monkeypatch):
    _setup_device(monkeypatch)
    _printing_job()
    alerts.evaluate()
    assert db.is_alert_open("R6", DEV)
    assert "出纸异常" in _r6_message()


def test_r6_not_triggered_while_printing_is_recent(temp_db, monkeypatch):
    _setup_device(monkeypatch)
    _printing_job(seconds_in_printing=10)
    alerts.evaluate()
    assert not db.is_alert_open("R6", DEV)


def test_r6_closes_after_eject_event(temp_db, monkeypatch):
    _setup_device(monkeypatch)
    job_id = _printing_job()
    alerts.evaluate()
    assert db.is_alert_open("R6", DEV)

    db.add_eject_event(DEV, {"event_id": "ev-r6-1", "event": "sheet_out",
                             "source": "x5", "ts": 1790000000,
                             "confidence": 0.9}, order_id=job_id)
    alerts.evaluate()
    assert not db.is_alert_open("R6", DEV)


def test_r6_skipped_when_device_offline(temp_db, monkeypatch):
    _setup_device(monkeypatch, heartbeat_seconds_ago=120)
    _printing_job()
    alerts.evaluate()
    # 离线由 R2 负责，不再重复报出纸异常
    assert db.is_alert_open("R2", DEV)
    assert "出纸异常" not in _r6_message()
