"""告警 API。

认证方式与 routers/printer.py 一致（SECRET_KEY）。
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends

import db
import notify
import ops_agent
from jwt import verify_admin_key
from models import ReturnResult

router = APIRouter()


@router.get("/api/alert/list")
def alert_list(resolved: Optional[bool] = None, role: str = Depends(verify_admin_key)):
    """告警列表。resolved 不传返回全部；false 只看未处理；true 只看已处理。"""
    alerts = db.list_alerts(resolved=resolved)
    return ReturnResult(200, "success", {"alerts": alerts})


@router.post("/api/alert/{alert_id}/resolve")
def alert_resolve(alert_id: int, role: str = Depends(verify_admin_key)):
    """把指定告警标记为已处理。"""
    updated = db.resolve_alert(alert_id)
    if not updated:
        return ReturnResult(200, "alert not found or already resolved", {})
    return ReturnResult(200, "success", {"id": alert_id})


@router.post("/api/alert/test-push")
def alert_test_push(role: str = Depends(verify_admin_key)):
    """手动推一条测试消息到企微 Webhook，返回真实结果（现场验证 / 演示用）。

    未配置 WECOM_WEBHOOK_URL 时如实返回 push failed + "webhook not configured"，
    不会假装推送成功。
    """
    ok, detail = notify.send_text(
        "【思印客】告警推送测试\n"
        "收到这条消息说明企微群机器人 Webhook 已接通。\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return ReturnResult(
        200, "success" if ok else "push failed",
        {"configured": notify.is_configured(), "detail": detail},
    )


# ----------------------- 运维 agent -----------------------


@router.get("/api/agent/runs")
def agent_runs(device_id: Optional[str] = None, limit: int = 50,
               role: str = Depends(verify_admin_key)):
    """运维 agent 处置记录：证据 → 诊断 → 动作 的留痕，最新在前。

    商家与运维都可查看（运维排障需要看设备处置历史）。
    """
    limit = max(1, min(limit, 200))
    return ReturnResult(200, "success", {
        "runs": db.list_agent_runs(device_id=device_id, limit=limit),
    })


@router.post("/api/agent/scan")
def agent_scan(role: str = Depends(verify_admin_key)):
    """手动跑一轮运维 agent（现场验证 / 演示用），返回本轮真实处置结果。

    物理故障只上报 + 人工；在线设备打印链路假死才自动重启自愈。
    """
    result = ops_agent.scan()
    return ReturnResult(200, "success", result)
