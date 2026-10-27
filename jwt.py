import hmac
import random
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from ctypes import Union
from datetime import datetime, timezone, timedelta

from jose import JWTError, jwt, ExpiredSignatureError
import uuid

from setting import SECRET_KEY, OPS_KEY, ALGORITHM, USER_TOKEN_DAYS
import db


def secret_key_matches(candidate):
    """常量时间比较候选密钥与 SECRET_KEY，避免通过响应时间侧信道逐字节推断密钥。

    供打印机端/管理端接口复用，替代裸 == 比较。空值直接判否。
    """
    if not candidate:
        return False
    return hmac.compare_digest(str(candidate), str(SECRET_KEY))


def ops_key_matches(candidate):
    """运维密钥比较；OPS_KEY 为空表示生产未配置，运维登录关闭。"""
    if not candidate or not OPS_KEY:
        return False
    return hmac.compare_digest(str(candidate), str(OPS_KEY))


def role_of_key(candidate):
    """返回 merchant / ops / None。

    商家密钥拥有全部管理权限；运维密钥只能看设备与告警、打印测试页、重启客户端。
    注意：租户密钥不在平台角色内（role_of_key 返回 None），
    租户只能用 verify_tenant_key 访问自己租户的接口，避免越权看全平台数据。
    """
    if secret_key_matches(candidate):
        return "merchant"
    if ops_key_matches(candidate):
        return "ops"
    return None


def tenant_of_key(candidate):
    """用租户密钥反查租户（含订阅）；无匹配返回 None。"""
    return db.get_tenant_by_secret(candidate)


def create_token():
    # 60 分钟：用户从上传、调参数到付款往往超过 15 分钟，
    # 过期太短会导致上传/下单突然 401；文件目录仍有 15 分钟不活跃清理。
    expire = datetime.now().timestamp() + 60 * 60
    payload_uuid = str(uuid.uuid4())
    to_encode = {"token": payload_uuid, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_user_token(user_id, session_token=None):
    """登录 token：带账号 uid 与本次上传会话目录 token，有效期 USER_TOKEN_DAYS 天。"""
    expire = datetime.now().timestamp() + 60 * 60 * 24 * USER_TOKEN_DAYS
    to_encode = {
        "token": session_token or str(uuid.uuid4()),
        "uid": int(user_id),
        "exp": expire,
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


security = HTTPBearer()


def verify_admin_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """管理端通用鉴权：商家或运维密钥都放行，返回角色。"""
    role = role_of_key(credentials.credentials)
    if not role:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    return role


def verify_merchant_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """商家专属鉴权：价格、账单、收款确认等只允许商家密钥。"""
    role = role_of_key(credentials.credentials)
    if role != "merchant":
        raise HTTPException(status_code=403, detail="merchant key required")
    return role


def verify_device_or_merchant(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """打印机端鉴权：商家密钥（老部署）或设备令牌（零配置接入）都放行。

    返回值：merchant 或 device:<device_id>。设备令牌只能操作自己这台设备，
    具体接口再校验 device_id 是否匹配，避免一台终端操作别的机器。
    """
    if secret_key_matches(credentials.credentials):
        return "merchant"
    device_id = db.device_id_by_token(credentials.credentials)
    if device_id:
        return "device:" + device_id
    raise HTTPException(status_code=401, detail="Invalid device token")


def auth_device_id(scope):
    """从鉴权结果里取出设备令牌绑定的 device_id；商家密钥返回 None。"""
    if isinstance(scope, str) and scope.startswith("device:"):
        return scope[len("device:"):]
    return None


def verify_tenant_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """租户密钥鉴权：返回租户字典（含 tenant_id 与订阅信息）。

    租户密钥只能访问 /api/tenant/self/* 这类「只看自己租户」的接口；
    平台级接口（设备列表、全局定价、账单等）一律拒绝租户密钥，
    把 role_of_key 与 verify_tenant_key 分开就是为了这道边界。
    """
    tenant = db.get_tenant_by_secret(credentials.credentials)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid tenant key")
    return tenant


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """用户端鉴权入口：现在统一要求已登录且账号未被封禁。

    保留函数名以兼容现有路由；真正的校验在 verify_user（token 带 uid + 查库）。
    """
    return verify_user(credentials)


def verify_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """已登录用户鉴权：token 必须带 uid，且账号存在、未被封禁。

    每次请求都查一次 users 表，管理员封禁后已登录的旧 token 立即失效（403）。
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="请先登录")
    uid = payload.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    user = db.get_user(int(uid))
    if user is None:
        raise HTTPException(status_code=401, detail="账号不存在，请重新登录")
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="账号已被封禁")
    return payload


def renew_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except ExpiredSignatureError:
        return create_token()
    else:
        if payload.get("exp") > datetime.now().timestamp():
            # 账号 token 续签保持 7 天；匿名会话仍是 60 分钟
            if payload.get("uid"):
                expire = datetime.now(timezone.utc) + timedelta(days=USER_TOKEN_DAYS)
            else:
                expire = datetime.now(timezone.utc) + timedelta(minutes=60)
            to_encode = {"token": payload.get("token"), "exp": expire}
            # 账号 token 续签时必须保留 uid，否则续签后会被当成未登录
            if payload.get("uid"):
                to_encode["uid"] = payload.get("uid")
            encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
            return encoded_jwt
        else:
            return create_token()


def decode_token(token: str):
    # 同样限定算法白名单；仅关闭过期校验，用于续签场景
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={'verify_exp': False})


def decode_token_strict(token: str):
    """严格解码：校验签名、过期时间与算法白名单。

    用于放在 URL 里的会话 token（预览、缩略图）。不能复用 decode_token，
    后者关闭了过期校验，等于 token 永不过期。
    """
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])