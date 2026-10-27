"""交付承诺：超时免单与收款流水状态流转。"""

from datetime import datetime, timedelta

import pytest

import db


def _make_job(deadline, out_trade_no="T1", folder="F1", filename="a.pdf"):
    return db.add_job({
        "folder": folder,
        "filename": filename,
        "print_copies": 1,
        "print_range_start": 1,
        "print_range_end": 1,
        "print_side": "one-sided",
        "display_name": "a.pdf",
        "amount": 0.3,
        "out_trade_no": out_trade_no,
        "eta_seconds": 60,
        "eta_deadline": deadline,
    })


def _past():
    return (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")


def _future():
    return (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")


def test_not_overdue_no_waiver(temp_db):
    _make_job(_future())
    db.add_payment("T1", 0.3, status="submitted")
    db.complete_job(folder="F1", filename="a.pdf")
    assert db.apply_guarantee("F1", "a.pdf") is None


def test_overdue_submitted_payment_becomes_waived(temp_db):
    job_id = _make_job(_past())
    db.add_payment("T1", 0.3, status="submitted")
    db.complete_job(folder="F1", filename="a.pdf")
    result = db.apply_guarantee("F1", "a.pdf")
    assert result["waived"] is True and result["overtime"] is True
    job = db.get_job_by_id(job_id)
    assert job["waived"] == 1 and job["overtime"] == 1
    assert "超过承诺完成时间" in job["waive_reason"]
    assert db.list_payments()[0]["status"] == "waived"


def test_overdue_confirmed_payment_becomes_refund_pending(temp_db):
    _make_job(_past(), out_trade_no="T2")
    db.add_payment("T2", 0.3, status="submitted")
    payment_id = db.list_payments()[0]["id"]
    db.confirm_payment(payment_id)
    db.complete_job(folder="F1", filename="a.pdf")
    result = db.apply_guarantee("F1", "a.pdf")
    assert result["payment_status"] == "refund_pending"
    assert db.list_payments()[0]["status"] == "refund_pending"


def test_waiver_is_idempotent(temp_db):
    _make_job(_past())
    db.add_payment("T1", 0.3, status="submitted")
    db.complete_job(folder="F1", filename="a.pdf")
    db.apply_guarantee("F1", "a.pdf")
    again = db.apply_guarantee("F1", "a.pdf")
    assert again["waived"] is True
    assert "payment_status" not in again


def test_waived_payment_cannot_be_confirmed(temp_db):
    _make_job(_past())
    db.add_payment("T1", 0.3, status="submitted")
    db.complete_job(folder="F1", filename="a.pdf")
    db.apply_guarantee("F1", "a.pdf")
    payment_id = db.list_payments()[0]["id"]
    assert db.confirm_payment(payment_id) is False
