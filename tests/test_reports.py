"""统计报表：汇总口径、筛选、CSV 导出。"""

import csv
import io

import db
from routers.admin import report_daily_csv, report_orders_csv, report_summary


def _add_job(**overrides):
    job = {
        "folder": "S1", "filename": "a.pdf", "print_copies": 1,
        "print_range_start": 1, "print_range_end": 10,
        "print_side": "one-sided", "status": "completed",
        "amount": 2.0, "out_trade_no": "T1", "print_color": "bw",
        "paper": "A4",
    }
    job.update(overrides)
    job_id = db.add_job(job)
    if overrides.get("created_at"):
        conn = db._connect()
        try:
            conn.execute("UPDATE print_jobs SET created_at=? WHERE id=?",
                         (overrides["created_at"], job_id))
            conn.commit()
        finally:
            conn.close()
    return job_id


def test_report_summary_math(temp_db):
    db.register_device("dev-1", "一楼", "Canon", "", "", 250, tenant_id="default")
    _add_job(created_at="2026-10-01 10:00:00", print_copies=2,
             print_range_end=10, print_side="one-sided",
             amount=2.0, saved_amount=0.5)
    _add_job(created_at="2026-10-02 10:00:00", print_copies=1,
             print_range_end=10, print_side="two-sided-long-edge", amount=3.0)
    _add_job(created_at="2026-10-02 11:00:00", status="failed",
             out_trade_no=None, amount=0)
    _add_job(created_at="2026-10-03 11:00:00", amount=9.0, waived=1)

    data = db.report_summary(start="2026-10-01", end="2026-10-03")
    summary = data["summary"]
    assert summary["orders_total"] == 4
    assert summary["orders_completed"] == 3
    assert summary["orders_failed"] == 1
    # 营收只算已支付且未免单：2.0 + 3.0，第三单 waived 排除
    assert summary["orders_paid"] == 2
    assert summary["revenue"] == 5.0
    assert summary["avg_price"] == 2.5
    # 页数/纸张只算已完成：10×2 + 10 + 10（已免单但完成）
    assert summary["pages"] == 40
    assert summary["sheets"] == 35
    assert summary["completion_rate"] == 75.0
    assert [d["date"] for d in data["daily"]] == [
        "2026-10-01", "2026-10-02", "2026-10-03"]
    assert data["by_device"][0]["device_id"] == "未指定"
    assert data["by_tenant"][0]["tenant_id"] == "default"


def test_report_filters(temp_db):
    db.register_device("dev-a", "A 点", "HP", "", "", 250, tenant_id="default")
    db.register_device("dev-b", "B 点", "Canon", "", "", 250, tenant_id="default")
    _add_job(created_at="2026-10-01 10:00:00", printer_id="dev-a", folder="A1")
    _add_job(created_at="2026-10-05 10:00:00", printer_id="dev-b", folder="B1")

    only_a = db.report_summary(start="2026-10-01", end="2026-10-05",
                               device_id="dev-a")
    assert only_a["summary"]["orders_total"] == 1
    assert only_a["by_device"][0]["name"] == "A 点"

    only_day = db.report_summary(start="2026-10-05", end="2026-10-05")
    assert only_day["summary"]["orders_total"] == 1
    assert only_day["daily"][0]["date"] == "2026-10-05"


def test_csv_exports(temp_db):
    _add_job(created_at="2026-10-01 10:00:00", print_copies=2,
             print_range_end=5, amount=1.5)
    daily = report_daily_csv(start="2026-10-01", end="2026-10-01",
                             role="merchant")
    rows = list(csv.reader(io.StringIO(daily.body.decode("utf-8-sig"))))
    assert rows[0][0] == "日期"
    assert rows[1][:4] == ["2026-10-01", "1", "1", "10"]

    orders = report_orders_csv(start="2026-10-01", end="2026-10-01",
                               role="merchant")
    rows = list(csv.reader(io.StringIO(orders.body.decode("utf-8-sig"))))
    assert rows[0][0] == "订单ID"
    assert len(rows) == 2
    assert rows[1][7] == "2"       # 份数
    assert rows[1][8] == "10"      # 总页数 = 5 页 × 2 份


def test_invalid_date_rejected(temp_db):
    result = report_summary(start="2026/10/01", role="merchant")
    assert "日期格式" in result.message
    assert result.data == {}
