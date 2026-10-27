"""零配置接入：配对申请 → 管理端批准 → 客户端拿令牌 → 令牌鉴权边界。"""

from types import SimpleNamespace

import db
from routers import device as device_router
from routers.device import (ClaimApprove, DeviceClaimRequest, device_claim,
                            device_claim_approve, device_claim_list,
                            device_claim_reject, device_claim_status,
                            device_register, DeviceRegister, device_heartbeat,
                            DeviceHeartbeat)


def _start_claim(temp_db):
    device_router._CLAIM_HITS.clear()
    result = device_claim(DeviceClaimRequest(
        hostname="print-pc-01", fingerprint="print-pc-01-abcdef",
        model="Canon G3800 series", printer_name="Canon G3800 series",
        printers=[{"name": "Canon G3800 series", "driver": "cnijfilter2",
                   "port": "usb://Canon/G3800", "supports_color": True,
                   "supports_duplex": False}],
        paper_capacity=250,
    ), http_request=SimpleNamespace(client=SimpleNamespace(host="10.0.0.1")))
    assert result.message == "success"
    return result.data


def test_claim_approve_flow(temp_db):
    claim = _start_claim(temp_db)
    assert len(claim["code"]) == 4

    listed = device_claim_list(role="merchant").data["claims"]
    assert [c["claim_id"] for c in listed] == [claim["claim_id"]]
    assert listed[0]["printers"][0]["name"] == "Canon G3800 series"

    approved = device_claim_approve(
        claim["claim_id"],
        ClaimApprove(tenant_id="default", name="一楼大厅 · 佳能",
                     address="图书馆一层", lat=39.9042, lng=116.4074,
                     paper_capacity=250),
        role="merchant",
    )
    assert approved.message == "success"
    device_id = approved.data["device_id"]

    device = db.get_device(device_id)
    assert device["name"] == "一楼大厅 · 佳能"
    assert device["address"] == "图书馆一层"
    assert device["lat"] == 39.9042
    # 设备能力来自客户端上报的队列，管理端不用再填
    assert device["supports_color"] is True
    assert device["supports_duplex"] is False

    # 客户端拿一次令牌，再取就没有了（防止被重复领走）
    polled = device_claim_status(claim["claim_id"], secret=claim["secret"])
    assert polled.data["status"] == "bound"
    assert polled.data["device_id"] == device_id
    assert polled.data["token"]
    second = device_claim_status(claim["claim_id"], secret=claim["secret"])
    assert second.data["status"] == "token_taken"
    assert db.device_id_by_token(polled.data["token"]) == device_id


def test_claim_requires_correct_secret(temp_db):
    claim = _start_claim(temp_db)
    wrong = device_claim_status(claim["claim_id"], secret="not-the-secret")
    assert wrong.message == "invalid secret"
    assert wrong.data == {}


def test_reject_claim_keeps_device_out(temp_db):
    claim = _start_claim(temp_db)
    assert device_claim_reject(claim["claim_id"], role="merchant").message == "success"
    listed = device_claim_list(role="merchant").data["claims"]
    assert listed == []
    assert db.list_devices() == []


def test_device_token_cannot_control_other_device(temp_db):
    claim = _start_claim(temp_db)
    approved = device_claim_approve(claim["claim_id"], ClaimApprove(name="点位"),
                                    role="merchant")
    device_id = approved.data["device_id"]
    token = device_claim_status(claim["claim_id"], secret=claim["secret"]).data["token"]
    scope = "device:" + device_id

    # 自己的设备可以注册/心跳
    ok = device_register(DeviceRegister(device_id=device_id, name="点位"), scope=scope)
    assert ok.message == "success"
    beat = device_heartbeat(DeviceHeartbeat(device_id=device_id), scope=scope)
    assert beat.message == "success"

    # 换成别人的 device_id 必须 403
    denied = device_register(DeviceRegister(device_id="dev-other"), scope=scope)
    assert denied.status_code == 403
    denied_beat = device_heartbeat(DeviceHeartbeat(device_id="dev-other"), scope=scope)
    assert denied_beat.status_code == 403

    # 设备令牌不是管理端密钥，不能看设备列表
    assert db.device_id_by_token(token) == device_id


def test_device_token_cannot_change_tenant(temp_db):
    claim = _start_claim(temp_db)
    approved = device_claim_approve(
        claim["claim_id"], ClaimApprove(tenant_id="default"), role="merchant")
    device_id = approved.data["device_id"]
    scope = "device:" + device_id
    device_register(DeviceRegister(device_id=device_id, tenant_id="other-tenant"),
                    scope=scope)
    assert db.get_device(device_id)["tenant_id"] == "default"
