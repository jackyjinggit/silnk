"""运维 agent：按故障类型分档处置。

边界写死在代码里，只做两类事：

一、物理故障（缺纸 / 卡纸 / 断电 / 门磁 / 取件顺序不符）—— 只能上报，必须人工到现场。
    自动重启解决不了纸盒空了，还可能把卡住的纸撕坏。这类 agent 只把告警翻译成
    具体处置动作并落库留痕；企微推送继续由告警引擎 alerts.py 负责，agent 不重复推送，
    避免同一条故障在群里刷两条消息。

二、打印链路假死 —— 设备心跳仍正常、但订单长时间卡在「打印中」。
    心跳正常说明客户端主进程还活着，卡住的是打印链路（CUPS / ipptool / SumatraPDF），
    这时下发 restart 让客户端自愈是安全的。
    重启后当前这张订单不会自动续打（任务已出队），因此同时把卡住的订单如实标 failed，
    让用户端显示「打印中断」并走重印申请，而不是傻等 30 分钟兜底超时。

⚠ 设备离线（心跳超时）不自动重启：客户端进程已经不在了，命令下发不到，
   10 分钟 TTL 过期后连执行痕迹都没有，只会制造「已下发」的假象——归入第一类，人工现场。
"""

import time
from datetime import datetime

import db
import notify

# 与告警引擎、设备页共用同一离线阈值（60 秒），避免三处各写一个数
from alerts import OFFLINE_TIMEOUT_SECONDS

# 卡在「打印中」超过 5 分钟即判定打印链路假死。
# 必须早于 db.fail_stale_printing 的 30 分钟兜底，否则订单先被标 failed，agent 就没机会介入。
STALE_PRINTING_SECONDS = 300
# 同一设备 30 分钟内最多自愈重启一次，避免 agent 自己把自己卷进重启循环
RESTART_COOLDOWN_SECONDS = 1800

# 物理故障规则 → (处置动作, 说明)。这些是「人才能解决」的问题，agent 只上报。
PHYSICAL_RULES = {
    "R1": ("现场补纸", "纸盒缺纸/少纸，软件无法补纸，需现场加纸"),
    "R2": ("现场检查电源与网络",
           "设备离线：客户端进程已不在，重启命令下发不到，需现场检查电源与网络"),
    "R3": ("现场确认供电",
           "市电中断、UPS 供电中，需现场确认供电；此时自动重启会中断正在打印的任务"),
    "R4": ("现场开盖检查",
           "门磁异常，可能卡纸或开盖，需现场处理——自动重启可能撕坏卡住的纸张"),
    "R5": ("现场核对", "取件顺序不符，可能有人拿错，需现场核对"),
}


def _effective_status(device):
    """按心跳时间现算在线状态，与 routers/device.py 的 _effective_status 同阈值。"""
    last = (device or {}).get("last_heartbeat")
    if not last:
        return "offline"
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "offline"
    if (datetime.now() - last_dt).total_seconds() > OFFLINE_TIMEOUT_SECONDS:
        return "offline"
    return "online"


def _cooldown_left(device_id):
    """距离上次自愈重启还有多少秒冷却；可以立刻重启时返回 0。"""
    last = db.last_agent_action_at(device_id, "restart")
    if not last:
        return 0
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    left = RESTART_COOLDOWN_SECONDS - (datetime.now() - last_dt).total_seconds()
    return max(int(left), 0)


def _report_physical():
    """第一类：把未处理的物理类告警翻译成处置动作并落库（不推企微、不动设备）。"""
    handled = []
    for alert in db.list_alerts(resolved=False):
        rule = (alert.get("rule") or "").upper()
        if rule not in PHYSICAL_RULES:
            continue
        if db.has_agent_run_for_alert(alert.get("id")):
            continue
        action, reason = PHYSICAL_RULES[rule]
        db.add_agent_run(
            kind="physical",
            device_id=alert.get("device_id"),
            alert_id=alert.get("id"),
            verdict="need_manual",
            reason=f"{rule}：{reason}",
            action=action,
            action_status="reported",
        )
        handled.append(alert.get("id"))
    return handled


def _selfheal_hung_printing():
    """第二类：在线设备上卡死的打印链路 → 自动下发 restart + 把该单如实标 failed。"""
    healed = []
    for job in db.list_stale_printing_jobs(STALE_PRINTING_SECONDS):
        device_id = job.get("printer_id")
        folder, filename = job.get("folder"), job.get("filename")

        if not device_id:
            # 订单没绑定设备，定位不到是哪台机器卡的，只能人工看
            if not db.has_agent_run("", "report_only", folder, filename):
                db.add_agent_run(
                    kind="software", device_id="",
                    job_folder=folder, job_filename=filename,
                    verdict="unknown_device",
                    reason="订单卡在打印中，但下单时未指定打印机、也无设备认领过，无法定位机器",
                    action="report_only", action_status="reported")
            continue

        device = db.get_device(device_id)
        if device is None:
            continue
        if _effective_status(device) == "offline":
            # 客户端进程已不在，命令下发不到；留给第一类人工处理
            continue
        if db.has_agent_run(device_id, "restart", folder, filename):
            continue
        left = _cooldown_left(device_id)
        if left > 0:
            if not db.has_agent_run(device_id, "restart_cooldown", folder, filename):
                db.add_agent_run(
                    kind="software", device_id=device_id,
                    job_folder=folder, job_filename=filename,
                    verdict="cooldown",
                    reason=f"该设备 {left} 秒前刚自愈重启过，本次只记录不重复重启（防重启循环）",
                    action="restart_cooldown", action_status="skipped")
            continue

        command_id = db.add_command(device_id, "restart")
        run_id = db.add_agent_run(
            kind="software", device_id=device_id,
            job_folder=folder, job_filename=filename,
            verdict="hung_printing",
            reason=(f"设备心跳正常但订单卡在打印中超过 {STALE_PRINTING_SECONDS} 秒，"
                    "判定打印链路假死，下发重启自愈"),
            action="restart", action_status="sent", command_id=command_id)
        # 重启不会续打已出队的任务：如实标 failed，让用户端显示打印中断并可申请重印
        db.set_job_status(folder, filename, "failed")
        db.set_agent_run_command(run_id, command_id, "sent")

        notify.send_text(
            "【思印客】运维 agent 自动处置\n"
            "设备：" + str(device.get("name") or device_id) + "\n"
            "诊断：打印链路假死（心跳正常、订单卡在打印中）\n"
            "动作：已下发重启客户端指令\n"
            "说明：重启不会续打当前这一单，订单已如实标为打印失败，用户可申请重印。\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        healed.append(device_id)
    return healed


def scan():
    """跑一轮运维 agent：先上报物理故障，再尝试自愈打印链路假死。"""
    physical = _report_physical()
    healed = _selfheal_hung_printing()
    return {"reported_alerts": physical, "selfhealed_devices": healed}
