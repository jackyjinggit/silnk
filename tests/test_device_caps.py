import asyncio

import db
from models import FileList
from routers import pay


def test_register_new_device_records_caps(temp_db):
    device = db.register_device(
        "dev-g3800", "一楼打印点", "Canon G3800 series", "", "usb://Canon/G3800",
        100, None, None,
        supports_color=True, supports_duplex=False)
    assert device["model"] == "Canon G3800 series"
    assert device["supports_color"] is True
    assert device["supports_duplex"] is False


def test_register_keeps_manual_caps(temp_db):
    db.register_device("dev-2", "机", "Canon G3800 series", "", "", 100,
                       supports_color=True, supports_duplex=True)
    # 管理端手动关掉双面
    db.update_device_config("dev-2", supports_duplex=False)
    # 设备下次启动重新注册（PPD 说支持双面）不应覆盖管理端设置
    db.register_device("dev-2", "机", "Canon G3800 series", "", "", 100,
                       supports_color=True, supports_duplex=True)
    device = db.get_device("dev-2")
    assert device["supports_duplex"] is False


def test_register_without_caps_keeps_none(temp_db):
    device = db.register_device("dev-3", "机", "未知型号", "", "", 100)
    assert device["supports_color"] is None
    assert device["supports_duplex"] is None


def test_g3800_rejects_duplex_bill(temp_db):
    """不支持双面的机器（G3800）必须拦下双面单，避免付双面价单面出纸。"""
    db.register_device("dev-g3800", "G3800", "Canon G3800 series", "", "",
                       100, supports_color=True, supports_duplex=False)
    body = FileList(fileList=[{
        "filename": "a.pdf", "print_side": "two-sided-long-edge",
        "print_copies": 1, "print_range_start": 1, "print_range_end": 1,
    }], printer_id="dev-g3800")
    result = asyncio.run(pay.create_bill(body, {"token": "s1", "uid": 1}))
    assert "不支持自动双面" in result.message
