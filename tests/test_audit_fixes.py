"""回归测试：代码审计中修复的高风险问题。"""

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import automatic_tasks
import db
from models import UpdateState
from routers.device import ClaimApprove, DeviceClaimRequest, device_claim, device_claim_approve
from routers.printer import CommandResult, printer_command_result, update_job_status


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"%PDF-1.4\n%%EOF")


def test_collecting_group_files_are_not_deleted(temp_db, tmp_path, monkeypatch):
    """AA 锁定后仍在收款：定时清理不能删成员文件。"""
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "uploads" / "group_ABC" / "converted" / "a.pdf")
    db.create_group("ABC", "发起人", "sess-1", pay_mode="aa", owner_uid=1)
    db.update_group("ABC", status="collecting")
    automatic_tasks.clean_finished_groups()
    assert (tmp_path / "uploads/group_ABC/converted/a.pdf").exists()


def test_paid_group_files_keep_24h(temp_db, tmp_path, monkeypatch):
    """已完成的拼单也不能因为 settled_at 为空就立刻删。"""
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "uploads" / "group_XYZ" / "converted" / "b.pdf")
    db.create_group("XYZ", "发起人", "sess-2", pay_mode="aa", owner_uid=1)
    db.update_group("XYZ", status="paid")
    automatic_tasks.clean_finished_groups()
    assert (tmp_path / "uploads/group_XYZ/converted/b.pdf").exists()


def test_printed_sources_skip_group_folders(temp_db, tmp_path, monkeypatch):
    """某个成员打印完成不能触发删除整个 AA 拼单目录。"""
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "uploads" / "group_PQR" / "converted" / "c.pdf")
    db.add_job({"folder": "group_PQR", "filename": "c.pdf",
                "print_copies": 1, "print_range_start": 1,
                "print_range_end": 1, "status": "completed"})
    conn = db._connect()
    try:
        conn.execute("UPDATE print_jobs SET status_at=?", ("2000-01-01 00:00:00",))
        conn.commit()
    finally:
        conn.close()
    automatic_tasks.clean_printed_sources()
    assert (tmp_path / "uploads/group_PQR/converted/c.pdf").exists()


def test_device_token_cannot_update_unassigned_job(temp_db, tmp_path, monkeypatch):
    """printer_id 为空的订单不能被任意设备令牌改成完成。"""
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "pending_files" / "S9" / "x.pdf")
    db.register_device("dev-a", "A", "HP", "", "", 250, tenant_id="default")
    db.add_job({"folder": "S9", "filename": "x.pdf", "print_copies": 1,
                "print_range_start": 1, "print_range_end": 1,
                "status": "pending", "amount": 1.0, "out_trade_no": "T9"})
    state = UpdateState(filename="x.pdf", folder="S9", state="success",
                        authentication="ignored")
    with pytest.raises(HTTPException) as exc:
        update_job_status(state, _auth="device:dev-a")
    assert exc.value.status_code == 403
    assert db.get_job("S9", "x.pdf")["status"] == "pending"


def test_device_token_cannot_finish_other_command(temp_db):
    """设备令牌不能改写别台设备的指令结果。"""
    db.register_device("dev-a", "A", "HP", "", "", 250, tenant_id="default")
    db.register_device("dev-b", "B", "Canon", "", "", 250, tenant_id="default")
    command_id = db.add_command("dev-b", "restart")
    with pytest.raises(HTTPException) as exc:
        printer_command_result(CommandResult(id=command_id, ok=True),
                               _auth="device:dev-a")
    assert exc.value.status_code == 403
    assert db.get_command(command_id)["status"] == "pending"


def test_group_collect_atomic(temp_db):
    """重复锁定只有第一次成功，避免生成两套成员账单。"""
    db.create_group("QQQ", "发起人", "sess-3", pay_mode="aa", owner_uid=2)
    assert db.claim_group_for_collect("QQQ") is True
    assert db.claim_group_for_collect("QQQ") is False


def test_claim_approve_uses_selected_printer(temp_db):
    """批准时按操作员选中的队列写能力，而不是列表第一台。"""
    result = device_claim(DeviceClaimRequest(
        hostname="pc", fingerprint="pc-1", model="",
        printer_name="Canon G3800",
        printers=[
            {"name": "HP LaserJet", "driver": "hplip", "port": "usb://HP",
             "supports_color": False, "supports_duplex": True},
            {"name": "Canon G3800", "driver": "cnijfilter2", "port": "usb://Canon",
             "supports_color": True, "supports_duplex": False},
        ],
        paper_capacity=250,
    ), http_request=SimpleNamespace(client=SimpleNamespace(host="10.0.0.9")))
    claim = result.data
    approved = device_claim_approve(claim["claim_id"], ClaimApprove(),
                                    role="merchant")
    device = db.get_device(approved.data["device_id"])
    assert device["driver"] == "cnijfilter2"
    assert device["supports_color"] is True
    assert device["supports_duplex"] is False
