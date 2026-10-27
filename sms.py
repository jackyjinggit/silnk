"""短信验证码发送。

真实短信服务商未接入时，绝不假装发送成功：
- SMS_PROVIDER 为空 → 返回「短信服务未接入」，注册页会如实提示；
- SMS_DEMO_MODE=1 → 仅本地/黑客松演示，验证码随接口返回并在页面标注「演示验证码」。

当前已实现阿里云短信（dysmsapi 2017-05-25），仅用标准库签名，不新增依赖。
需要的环境变量：SMS_PROVIDER=aliyun、SMS_ACCESS_KEY、SMS_ACCESS_SECRET、
SMS_SIGN_NAME（审核通过的签名）、SMS_TEMPLATE_CODE（含 ${code} 的模板）。
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import setting

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
# 两套阿里云接口：dysms 走短信服务（正式签名），dypns 走号码认证服务（可用测试签名）
ALIYUN_ENDPOINTS = {
    "dysms": "https://dysmsapi.aliyuncs.com/",
    "dypns": "https://dypnsapi.aliyuncs.com/",
}
ALIYUN_TIMEOUT = 8


def normalize_phone(phone):
    """去掉空格与 +86 前缀；格式不合法返回 None。"""
    value = re.sub(r"[\s\-]", "", str(phone or ""))
    if value.startswith("+86"):
        value = value[3:]
    elif value.startswith("86") and len(value) == 13:
        value = value[2:]
    return value if PHONE_RE.match(value) else None


def generate_code():
    """6 位数字验证码。

    用 secrets（密码学安全随机源）而非 random：random 是梅森旋转可预测，
    攻击者拿到若干历史验证码即可推断后续验证码，等于绕过短信二次校验。
    """
    return f"{secrets.randbelow(1000000):06d}"


def code_hash(phone, code):
    """验证码只存哈希，不落明文。"""
    return hashlib.sha256(f"{phone}:{code}".encode("utf-8")).hexdigest()


def _percent_encode(value):
    return (urllib.parse.quote(str(value), safe="~")
            .replace("+", "%20").replace("*", "%2A").replace("%7E", "~"))


def _aliyun_send(phone, code):
    """调用阿里云 SendSms；返回 (是否成功, 说明)。"""
    ak_id = (setting.SMS_ACCESS_KEY or "").strip()
    ak_secret = (setting.SMS_ACCESS_SECRET or "").strip()
    sign_name = (setting.SMS_SIGN_NAME or "").strip()
    template = (setting.SMS_TEMPLATE_CODE or "").strip()
    missing = [name for name, value in (
        ("SMS_ACCESS_KEY", ak_id),
        ("SMS_ACCESS_SECRET", ak_secret),
        ("SMS_SIGN_NAME", sign_name),
        ("SMS_TEMPLATE_CODE", template),
    ) if not value]
    if missing:
        return False, "阿里云短信配置不完整，缺少：" + "、".join(missing)

    params = {
        "AccessKeyId": ak_id,
        "Format": "JSON",
        "RegionId": "cn-hangzhou",
        "SignName": sign_name,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "TemplateCode": template,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
    }
    channel = (getattr(setting, "SMS_CHANNEL", "dysms") or "dysms").strip().lower()
    endpoint = ALIYUN_ENDPOINTS.get(channel)
    if endpoint is None:
        return False, f"未知的短信通道：{channel}"
    if channel == "dypns":
        params["Action"] = "SendSmsVerifyCode"
        params["PhoneNumber"] = phone
        params["TemplateParam"] = json.dumps({"code": code, "min": "5"},
                                             ensure_ascii=False)
        params["ReturnVerifyCode"] = "false"
    else:
        params["Action"] = "SendSms"
        params["PhoneNumbers"] = phone
        params["TemplateParam"] = json.dumps({"code": code}, ensure_ascii=False)

    canonical = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}"
                         for k, v in sorted(params.items()))
    string_to_sign = f"POST&%2F&{_percent_encode(canonical)}"
    digest = hmac.new(f"{ak_secret}&".encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    body = canonical + "&Signature=" + _percent_encode(base64.b64encode(digest).decode())

    request = urllib.request.Request(endpoint, data=body.encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=ALIYUN_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return False, f"阿里云短信失败：HTTP {exc.code}"
        return False, ("阿里云短信失败：" + str(detail.get("Code", "")) + " "
                       + str(detail.get("Message", ""))).strip()
    except Exception as exc:
        return False, f"阿里云短信请求异常：{exc}"

    if str(data.get("Code")) == "OK":
        return True, "aliyun"
    return False, ("阿里云短信失败：" + str(data.get("Code", "")) + " "
                   + str(data.get("Message", ""))).strip()


def send_code(phone, code):
    """发送验证码，返回 (是否成功, 说明)。

    未配置服务商时明确失败；演示模式只回 demo，不声称已发短信。
    """
    provider = (setting.SMS_PROVIDER or "").strip().lower()
    if not provider:
        if setting.SMS_DEMO_MODE:
            return True, "demo"
        return False, "短信服务未接入（见 /team 待完成），当前无法发送验证码"
    if provider == "aliyun":
        return _aliyun_send(phone, code)
    return False, f"短信服务商 {provider} 的调用尚未实现（见 /team 待完成）"
