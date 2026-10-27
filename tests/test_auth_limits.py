"""登录验证码限流与手机号脱敏。"""

import time

import setting
from routers import admin, auth


class _Client:
    def __init__(self, host):
        self.host = host


class _Request:
    def __init__(self, host="10.0.0.1"):
        self.client = _Client(host)


def _reset_ip():
    with auth._IP_LOCK:
        auth._IP_SENDS.clear()


def test_phone_hourly_quota(temp_db, monkeypatch):
    monkeypatch.setattr(setting, "SMS_DEMO_MODE", True)
    monkeypatch.setattr(setting, "SMS_PROVIDER", "")
    _reset_ip()
    phone = "13800138001"
    now = time.time()
    # 先塞 5 条历史记录，模拟一小时内的额度已用完
    for _ in range(auth.PHONE_HOURLY_LIMIT):
        db_id = temp_db.save_sms_code(phone, "hash", now + 300)
        assert db_id
    result = auth.send_code(auth.SendCode(phone=phone), _Request())
    assert "次数过多" in result.message


def test_ip_hourly_quota(temp_db, monkeypatch):
    monkeypatch.setattr(setting, "SMS_DEMO_MODE", True)
    _reset_ip()
    # 把 IP 配额临时调到 2，避免测试里发很多次
    monkeypatch.setattr(auth, "IP_HOURLY_LIMIT", 2)
    req = _Request("10.9.9.9")
    auth.send_code(auth.SendCode(phone="13800138002"), req)
    auth.send_code(auth.SendCode(phone="13800138003"), req)
    result = auth.send_code(auth.SendCode(phone="13800138004"), req)
    assert "网络请求过于频繁" in result.message


def test_mask_phone():
    assert admin._mask_phone("13800138000") == "138****8000"
    assert admin._mask_phone("") == ""
    assert admin._mask_phone(None) == ""
    assert admin._mask_phone("123") == "123"
