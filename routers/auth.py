"""用户端账号：手机号注册 / 密码登录 / 验证码登录。

设计口径（不编造）：
- 注册必须验证手机号；密码用 PBKDF2-SHA256 加盐哈希，数据库不存明文；
- 登录与注册分开：/api/auth/login 只做密码登录，/api/auth/register 只做注册；
- 验证码登录 /api/auth/verify 只允许已注册账号，不再自动注册；
- 真实短信服务商未接入时，send-code 明确返回「短信服务未接入」，不发假码；
- SMS_DEMO_MODE=1 时为演示模式，验证码随响应返回并带 demo=true 标记；
- 验证码 5 分钟有效，最多校验 5 次，同手机号 60 秒内只能发一次；
- 登录 token 带 uid，每次请求校验账号状态，封禁后旧 token 立即失效。
"""

import collections
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

import db
import jwt
import sms
from models import ReturnResult

router = APIRouter()

CODE_TTL_SECONDS = 300
CODE_MAX_ATTEMPTS = 5
SEND_INTERVAL_SECONDS = 60
# 同一手机号每小时最多发 5 条；同一 IP 每小时最多 10 条（防批量刷验证码）
PHONE_HOURLY_LIMIT = 5
IP_HOURLY_LIMIT = 10
# IP 计数放内存：单实例部署足够；多实例时每个实例各自限流，手机号配额仍由数据库兜底
_IP_SENDS = collections.defaultdict(collections.deque)
_IP_LOCK = threading.Lock()


def _client_ip(request):
    """取真实客户端 IP：经本机 nginx 反代时读 X-Forwarded-For 首个地址。"""
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


def _ip_allowed(ip):
    """IP 小时窗口限流；返回是否放行。"""
    if not ip:
        return True
    now = time.time()
    with _IP_LOCK:
        sends = _IP_SENDS[ip]
        while sends and now - sends[0] > 3600:
            sends.popleft()
        if len(sends) >= IP_HOURLY_LIMIT:
            return False
        sends.append(now)
        return True


class SendCode(BaseModel):
    phone: str


class VerifyCode(BaseModel):
    phone: str
    code: str
    nickname: str = ""


class RegisterRequest(BaseModel):
    """注册：手机号 + 验证码 + 密码（8–64 位）。"""

    phone: str
    code: str
    nickname: str = ""
    password: str


class PasswordLogin(BaseModel):
    phone: str
    password: str


class PasswordUpdate(BaseModel):
    """设置/修改密码：已设置过密码的账号必须提供原密码。"""

    old_password: str = ""
    new_password: str


class ProfileUpdate(BaseModel):
    nickname: str = ""


PBKDF2_ITERATIONS = 200_000
PASSWORD_MIN = 8
PASSWORD_MAX = 64
# 密码登录失败限流：同一手机号 + IP 10 分钟内最多失败 8 次
LOGIN_FAIL_LIMIT = 8
LOGIN_FAIL_WINDOW = 600
_LOGIN_FAILS = collections.defaultdict(collections.deque)
_LOGIN_LOCK = threading.Lock()


def hash_password(password):
    """PBKDF2-SHA256 加盐哈希，格式 pbkdf2_sha256$迭代次数$盐$摘要。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, salt, digest.hex())


def verify_password(password, stored):
    """常量时间校验密码哈希；格式损坏一律视为不匹配。"""
    try:
        algo, iterations, salt, expected = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt),
            int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


def _password_error(password):
    """返回密码不合规的提示，合规返回 None。"""
    if not password or len(password) < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 位"
    if len(password) > PASSWORD_MAX:
        return f"密码不能超过 {PASSWORD_MAX} 位"
    if password.strip() != password:
        return "密码前后不能有空格"
    return None


def _login_key(phone, ip):
    return f"{phone}|{ip or ''}"


def _login_blocked(key):
    now = time.time()
    with _LOGIN_LOCK:
        fails = _LOGIN_FAILS[key]
        while fails and now - fails[0] > LOGIN_FAIL_WINDOW:
            fails.popleft()
        return len(fails) >= LOGIN_FAIL_LIMIT


def _login_failed(key):
    with _LOGIN_LOCK:
        _LOGIN_FAILS[key].append(time.time())


def _login_succeeded(key):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(key, None)


def _seconds_since(ts):
    try:
        return (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return None


@router.post("/api/auth/send-code")
def send_code(request: SendCode, http_request: Request):
    """发送注册/登录验证码。未接入短信服务时如实返回失败。

    三层限流：同手机号 60 秒一次、同手机号每小时 5 条、同 IP 每小时 10 条。
    """
    phone = sms.normalize_phone(request.phone)
    if phone is None:
        return ReturnResult(200, "请输入正确的 11 位手机号", {})

    client_ip = _client_ip(http_request)
    if not _ip_allowed(client_ip):
        return ReturnResult(200, "当前网络请求过于频繁，请稍后再试", {})

    if db.count_recent_sms(phone, 3600) >= PHONE_HOURLY_LIMIT:
        return ReturnResult(200, "该手机号获取验证码次数过多，请 1 小时后再试", {})

    latest = db.get_latest_sms_code(phone)
    if latest:
        elapsed = _seconds_since(latest.get("created_at"))
        if elapsed is not None and elapsed < SEND_INTERVAL_SECONDS:
            wait = int(SEND_INTERVAL_SECONDS - elapsed)
            return ReturnResult(200, f"发送太频繁，请 {wait} 秒后再试", {})

    code = sms.generate_code()
    ok, detail = sms.send_code(phone, code)
    if not ok:
        # 未配置真实服务商：不保存、不返回验证码，避免造成「已发送」的错觉
        return ReturnResult(200, detail, {"configured": False})

    db.save_sms_code(phone, sms.code_hash(phone, code),
                     time.time() + CODE_TTL_SECONDS)
    data = {
        "configured": True,
        "demo": detail == "demo",
        "expires_in": CODE_TTL_SECONDS,
        "interval": SEND_INTERVAL_SECONDS,
    }
    if detail == "demo":
        # 演示模式明确把验证码交回前端，并标注未接入短信服务
        data["demo_code"] = code
        data["note"] = "演示模式：验证码直接显示，未接入短信服务"
    return ReturnResult(200, "success", data)


def _consume_sms_code(phone, code):
    """校验并消费验证码，返回 (是否通过, 错误提示)。"""
    code = (code or "").strip()
    if not code:
        return False, "请输入验证码"
    record = db.get_latest_sms_code(phone)
    if record is None:
        return False, "请先获取验证码"
    if (record.get("attempts") or 0) >= CODE_MAX_ATTEMPTS:
        return False, "验证码错误次数过多，请重新获取"
    if float(record.get("expires_at") or 0) < time.time():
        return False, "验证码已过期，请重新获取"
    expected = record.get("code_hash") or ""
    actual = sms.code_hash(phone, code)
    if not hmac.compare_digest(expected, actual):
        db.bump_sms_attempt(record["id"])
        return False, "验证码不正确"
    db.mark_sms_used(record["id"])
    return True, ""


def _login_result(user, is_new=False):
    """签发登录 token，返回与旧接口一致的响应结构。"""
    db.touch_user_login(user["id"])
    return ReturnResult(200, "success", {
        "token": jwt.create_user_token(user["id"]),
        "user": db.get_user(user["id"]),
        "is_new": is_new,
    })


@router.post("/api/auth/register")
def register(request: RegisterRequest):
    """注册新账号：必须验证手机号，并设置登录密码。

    老账号（早期只有验证码、没有密码）也走这里补设密码；
    已经设置过密码的账号会被要求直接登录，避免重置他人密码。
    """
    phone = sms.normalize_phone(request.phone)
    if phone is None:
        return ReturnResult(200, "请输入正确的 11 位手机号", {})
    error = _password_error(request.password)
    if error:
        return ReturnResult(200, error, {})
    existing = db.get_user_auth(phone)
    if existing and existing.get("password_hash"):
        return ReturnResult(200, "该手机号已注册，请直接登录", {})

    ok, message = _consume_sms_code(phone, request.code)
    if not ok:
        return ReturnResult(200, message, {})

    password_hash = hash_password(request.password)
    is_new = False
    if existing:
        db.set_user_password(existing["id"], password_hash)
        user = db.get_user(existing["id"])
    else:
        nickname = (request.nickname or "").strip() or f"用户{phone[-4:]}"
        user = db.create_user(phone, nickname, password_hash)
        if user is None:
            # 并发注册：另一个请求已经创建，按「补设密码」处理
            existing = db.get_user_auth(phone)
            if existing is None:
                return ReturnResult(200, "账号创建失败，请重试", {})
            if existing.get("password_hash"):
                return ReturnResult(200, "该手机号已注册，请直接登录", {})
            db.set_user_password(existing["id"], password_hash)
            user = db.get_user(existing["id"])
        else:
            is_new = True
    if user is None:
        return ReturnResult(200, "账号创建失败，请重试", {})
    if user.get("status") != "active":
        reason = user.get("ban_reason") or "违反平台规定"
        return ReturnResult(403, f"账号已被封禁：{reason}", {})
    return _login_result(user, is_new)


@router.post("/api/auth/login")
def login(request: PasswordLogin, http_request: Request):
    """密码登录。失败次数按手机号 + IP 限流，避免在线爆破。"""
    phone = sms.normalize_phone(request.phone)
    if phone is None:
        return ReturnResult(200, "请输入正确的 11 位手机号", {})
    if not request.password:
        return ReturnResult(200, "请输入密码", {})
    key = _login_key(phone, _client_ip(http_request))
    if _login_blocked(key):
        return ReturnResult(200, "密码错误次数过多，请 10 分钟后再试", {})

    user = db.get_user_auth(phone)
    if user is None or not user.get("password_hash") or not verify_password(
            request.password, user.get("password_hash")):
        _login_failed(key)
        if user is not None and not user.get("password_hash"):
            return ReturnResult(200, "该账号还没有设置密码，请用验证码登录后在账号页设置", {})
        return ReturnResult(200, "手机号或密码不正确", {})
    if user.get("status") != "active":
        reason = user.get("ban_reason") or "违反平台规定"
        return ReturnResult(403, f"账号已被封禁：{reason}", {})
    _login_succeeded(key)
    return _login_result(user)


@router.post("/api/auth/password")
def update_password(request: PasswordUpdate,
                    payload: dict = Depends(jwt.verify_user)):
    """设置或修改登录密码（已设置过密码时必须验证原密码）。"""
    user = db.get_user(int(payload.get("uid")))
    if user is None:
        return ReturnResult(200, "账号不存在", {})
    auth_user = db.get_user_auth(user["phone"])
    if auth_user and auth_user.get("password_hash"):
        if not verify_password(request.old_password,
                               auth_user.get("password_hash")):
            return ReturnResult(200, "原密码不正确", {})
    error = _password_error(request.new_password)
    if error:
        return ReturnResult(200, error, {})
    db.set_user_password(user["id"], hash_password(request.new_password))
    return ReturnResult(200, "success", {"has_password": True})


@router.post("/api/auth/verify")
def verify_code(request: VerifyCode):
    """验证码登录：只允许已注册账号，注册请走 /api/auth/register。"""
    phone = sms.normalize_phone(request.phone)
    if phone is None:
        return ReturnResult(200, "请输入正确的 11 位手机号", {})
    user = db.get_user_by_phone(phone)
    if user is None:
        return ReturnResult(200, "该手机号尚未注册，请先注册", {})

    ok, message = _consume_sms_code(phone, request.code)
    if not ok:
        return ReturnResult(200, message, {})

    if user.get("status") != "active":
        reason = user.get("ban_reason") or "违反平台规定"
        return ReturnResult(403, f"账号已被封禁：{reason}", {})
    return _login_result(user)


@router.get("/api/auth/me")
def me(payload: dict = Depends(jwt.verify_user)):
    """当前登录用户（封禁后这里会 403，前端据此退回登录页）。"""
    user = db.get_user(int(payload.get("uid")))
    return ReturnResult(200, "success", {"user": user})


@router.post("/api/auth/profile")
def update_profile(request: ProfileUpdate, payload: dict = Depends(jwt.verify_user)):
    """修改昵称（手机号是账号唯一标识，不支持自行修改）。"""
    nickname = (request.nickname or "").strip()
    if not nickname:
        return ReturnResult(200, "昵称不能为空", {})
    if len(nickname) > 20:
        return ReturnResult(200, "昵称太长（最多 20 字）", {})
    user = db.update_user_nickname(int(payload.get("uid")), nickname)
    if user is None:
        return ReturnResult(200, "账号不存在", {})
    return ReturnResult(200, "success", {"user": user})
