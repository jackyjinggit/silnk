"""账号：注册 / 密码登录 / 验证码登录 / 密码修改。"""

import time
from types import SimpleNamespace

import db
import sms
from routers import auth


def _give_code(phone, code="123456"):
    db.save_sms_code(phone, sms.code_hash(phone, code), time.time() + 300)
    return code


def _request(host="10.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_register_sets_password_and_logs_in(temp_db):
    phone = "13800138000"
    result = auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), nickname="小明", password="abc12345"))
    assert result.message == "success"
    assert result.data["user"]["has_password"] is True
    assert "password_hash" not in result.data["user"]

    stored = db.get_user_auth(phone)["password_hash"]
    assert stored.startswith("pbkdf2_sha256$")
    assert "abc12345" not in stored

    login = auth.login(auth.PasswordLogin(phone=phone, password="abc12345"),
                       _request())
    assert login.message == "success"
    assert login.data["token"]


def test_register_rejects_duplicate_with_password(temp_db):
    phone = "13800138001"
    auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), password="abc12345"))
    again = auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), password="another123"))
    assert "已注册" in again.message


def test_legacy_account_can_set_password_via_register(temp_db):
    """早期只有验证码的账号：注册页验证手机号后补设密码。"""
    phone = "13800138002"
    db.create_user(phone, "老用户")
    assert db.get_user_by_phone(phone)["has_password"] is False

    result = auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), password="abc12345"))
    assert result.message == "success"
    assert result.data["is_new"] is False
    assert db.get_user_by_phone(phone)["has_password"] is True


def test_password_login_rejects_wrong_and_missing(temp_db):
    phone = "13800138003"
    db.create_user(phone, "无密码")
    missing = auth.login(auth.PasswordLogin(phone=phone, password="whatever1"),
                         _request(host="10.0.0.3"))
    assert "还没有设置密码" in missing.message

    auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), password="abc12345"))
    wrong = auth.login(auth.PasswordLogin(phone=phone, password="wrongpass"),
                       _request(host="10.0.0.4"))
    assert wrong.message == "手机号或密码不正确"


def test_verify_code_does_not_auto_register(temp_db):
    phone = "13800138004"
    result = auth.verify_code(auth.VerifyCode(
        phone=phone, code=_give_code(phone)))
    assert "尚未注册" in result.message
    assert db.get_user_by_phone(phone) is None

    db.create_user(phone, "已有用户")
    ok = auth.verify_code(auth.VerifyCode(
        phone=phone, code=_give_code(phone)))
    assert ok.message == "success"

    me = auth.me({"uid": ok.data["user"]["id"]})
    assert "password_hash" not in me.data["user"]


def test_update_password_requires_old_when_set(temp_db):
    phone = "13800138005"
    auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), password="abc12345"))
    user = db.get_user_by_phone(phone)

    bad = auth.update_password(auth.PasswordUpdate(
        old_password="wrongpass", new_password="newpass123"), {"uid": user["id"]})
    assert "原密码不正确" in bad.message

    ok = auth.update_password(auth.PasswordUpdate(
        old_password="abc12345", new_password="newpass123"),
        {"uid": user["id"]})
    assert ok.message == "success"
    assert auth.login(auth.PasswordLogin(phone=phone, password="newpass123"),
                      _request(host="10.0.0.5")).message == "success"


def test_password_min_length(temp_db):
    phone = "13800138006"
    result = auth.register(auth.RegisterRequest(
        phone=phone, code=_give_code(phone), password="1234567"))
    assert "至少 8 位" in result.message
