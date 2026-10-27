"""运维 agent：物理故障只上报人工、在线设备打印链路假死自动重启自愈。"""

from datetime import datetime, timedelta

import db
import ops_agent

FMT = "%Y-%m-%d %H:%M:%S"


def _stale_job(folder="S1", filename="a.pdf", printer_id=None, minutes=10):
    """造一条卡在 printing 超过阈值的订单（status_at 回拨到过去）。"""
    jid = db.add_job({"folder": folder, "filename": filename, "print_copies": 1,
                      "print_range_start": 1, "print_range_end": 1,
                      "print_side": "one-sided", "status": "printing"},
                     printer_id=printer_id)
    back = (datetime.now() - timedelta(minutes=minutes)).strftime(FMT)
    conn = db._connect()
    try:
        conn.execute("UPDATE print_jobs SET status_at=? WHERE id=?", (back, jid))
        conn.commit()
    finally:
        conn.close()
    return jid


def _device(device_id, online=True):
    db.register_device(device_id, name=device_id, paper_capacity=250)
    if online:
        db.update_heartbeat(device_id)  # 刷新 last_heartbeat 为当前时间


def test_physical_alert_reported_without_acting(temp_db, monkeypatch):
    monkeypatch.setattr(ops_agent.notify, "send_text", lambda *a, **k: (False, "n/a"))
    _device("dev1")
    db.add_alert("R1", "dev1", "warning", "纸量不足")
    result = ops_agent.scan()
    assert len(result["reported_alerts"]) == 1
    run = temp_db.list_agent_runs("dev1")[0]
    assert run["kind"] == "physical"
    assert run["verdict"] == "need_manual"
    assert run["action_status"] == "reported"
    # 物理故障绝不下发指令
    assert temp_db.list_commands("dev1") == []


def test_physical_alert_not_reported_twice(temp_db):
    _device("dev2")
    db.add_alert("R4", "dev2", "warning", "门磁异常")
    ops_agent.scan()
    ops_agent.scan()
    assert temp_db.list_agent_runs("dev2")[0]["kind"] == "physical"
    assert len([r for r in temp_db.list_agent_runs("dev2")
                if r["alert_id"] is not None]) == 1


def test_offline_device_hung_job_not_restarted(temp_db):
    # 设备未刷新心跳 → 离线，命令下发不到，交人工
    db.register_device("dev3", name="dev3", paper_capacity=250)
    _stale_job(printer_id="dev3")
    result = ops_agent.scan()
    assert result["selfhealed_devices"] == []
    assert temp_db.list_commands("dev3") == []


def test_online_hung_printing_restarts_and_fails_job(temp_db, monkeypatch):
    sent = []
    monkeypatch.setattr(ops_agent.notify, "send_text",
                        lambda text, *a, **k: sent.append(text) or (True, "ok"))
    _device("dev4")
    _stale_job(printer_id="dev4")
    result = ops_agent.scan()
    assert result["selfhealed_devices"] == ["dev4"]
    run = temp_db.list_agent_runs("dev4")[0]
    assert run["kind"] == "software" and run["action"] == "restart"
    assert run["action_status"] == "sent" and run["command_id"]
    assert [c["command"] for c in temp_db.list_commands("dev4")] == ["restart"]
    # 重启不续打，订单如实标失败
    assert temp_db.list_jobs()[0]["status"] == "failed"
    assert sent  # 真实推送了处置通知


def test_restart_cooldown_skips_second_job(temp_db, monkeypatch):
    monkeypatch.setattr(ops_agent.notify, "send_text", lambda *a, **k: (False, "n/a"))
    _device("dev5")
    _stale_job(folder="S5", filename="a.pdf", printer_id="dev5")
    ops_agent.scan()
    _stale_job(folder="S5", filename="b.pdf", printer_id="dev5")
    result = ops_agent.scan()
    # 冷却期内不再重复重启
    assert result["selfhealed_devices"] == []
    assert len(temp_db.list_commands("dev5")) == 1
    assert any(r["action"] == "restart_cooldown" and r["action_status"] == "skipped"
               for r in temp_db.list_agent_runs("dev5"))


def test_job_without_device_reported_only(temp_db):
    _stale_job(folder="S6", filename="a.pdf", printer_id=None)
    result = ops_agent.scan()
    assert result["selfhealed_devices"] == []
    run = temp_db.list_agent_runs()[0]
    assert run["verdict"] == "unknown_device"
    assert run["action"] == "report_only" and run["action_status"] == "reported"
