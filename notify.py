"""企微（企业微信）群机器人 Webhook 告警推送。

只做一件事：把告警文本 POST 到企微机器人 Webhook。
Webhook 地址未配置时不做任何事，也绝不假装推送成功——返回值如实反映结果。

配置：环境变量 WECOM_WEBHOOK_URL（见 setting.py），形如
    https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
"""

import json
import queue
import threading
import time
import urllib.error
import urllib.request

import db
import setting

# 推送超时（秒）：不能太长，否则会拖住 10 秒一轮的告警扫描
TIMEOUT_SECONDS = 5
# 企微群机器人限流 20 条/分钟；这里做令牌窗口，超出就等待
RATE_LIMIT_PER_MINUTE = 20
# 单条告警最多尝试 3 次（首次 + 2 次重试）
MAX_ATTEMPTS = 3
# 启动补发时，失败超过这个次数的告警不再重试，避免死循环
MAX_RESEND_ATTEMPTS = 5

_queue = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False
_sent_times = []


def webhook_url():
    """当前生效的 Webhook 地址；未配置返回空串。"""
    return (setting.WECOM_WEBHOOK_URL or "").strip()


def is_configured():
    """是否已配置 Webhook。"""
    return bool(webhook_url())


def send_text(content, timeout=TIMEOUT_SECONDS):
    """同步推送一条文本消息，返回 (是否成功, 说明)。

    - 未配置 Webhook：返回 (False, "webhook not configured")，不假装成功；
    - 企微返回体里 errcode 非 0：返回 (False, "errcode=X errmsg=Y")；
    - 网络异常：返回 (False, 异常描述)。
    """
    url = webhook_url()
    if not url:
        return False, "webhook not configured"
    payload = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return False, f"request failed: {exc}"
    try:
        result = json.loads(body)
    except ValueError:
        # 非 JSON 响应（例如自建转发服务直接回 200）：按 HTTP 已送达处理
        return True, f"sent (non-json response: {body[:120]})"
    if isinstance(result, dict) and result.get("errcode"):
        return False, f"errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
    return True, "sent"


def alert_text(alert):
    """把一条告警记录拼成推送文本。"""
    level = "严重" if alert.get("level") == "critical" else "预警"
    return (
        f"【思印客告警】{level} · {alert.get('rule')}\n"
        f"设备：{alert.get('device_id')}\n"
        f"{alert.get('message')}\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


def _wait_for_rate_limit():
    """20 条/分钟令牌窗口：窗口满时阻塞到最早一条滑出。"""
    while True:
        now = time.time()
        while _sent_times and now - _sent_times[0] > 60:
            _sent_times.pop(0)
        if len(_sent_times) < RATE_LIMIT_PER_MINUTE:
            _sent_times.append(now)
            return
        time.sleep(max(1.0, 60 - (now - _sent_times[0])))


def _worker():
    """单后台线程串行发送：限流 + 重试，并把结果写回 alerts.push_status。"""
    while True:
        alert = _queue.get()
        ok, detail = False, ""
        for attempt in range(MAX_ATTEMPTS):
            _wait_for_rate_limit()
            ok, detail = send_text(alert_text(alert))
            if ok or detail == "webhook not configured":
                break
            time.sleep(2 ** attempt)
        try:
            db.mark_alert_push(alert.get("id"), ok, detail)
        except Exception as exc:  # 写状态失败不能影响后续告警
            print(f"[notify] 更新推送状态失败：{exc}", flush=True)
        _queue.task_done()


def push_alert(alert):
    """告警触发后入队推送（不阻塞告警扫描）；结果异步写回数据库。"""
    global _worker_started
    if not is_configured():
        try:
            db.mark_alert_push(alert.get("id"), False, "webhook not configured")
        except Exception:
            pass
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True).start()
            _worker_started = True
    _queue.put(alert)


def resend_unpushed_alerts():
    """启动补发：把「已落库但未推送成功」的未处理告警重新入队。

    只补发未处理告警，且失败次数超过 MAX_RESEND_ATTEMPTS 的不再重试。
    返回本次入队条数。
    """
    if not is_configured():
        return 0
    count = 0
    for alert in db.list_alerts_needing_push():
        if (alert.get("push_attempts") or 0) >= MAX_RESEND_ATTEMPTS:
            continue
        push_alert(alert)
        count += 1
    return count
