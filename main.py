import logging
import os.path
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

import setting
import db
import alerts
import instance_lock
import notify
import ops_agent
from models import GetJobsAccessLogFilter
from print_queue import print_queue_singleton
from routers import user, printer, device, alert, admin, team, auth
from routers import pay
from routers import group
from routers import tenant
from routers import library as library_router
from routers import display as display_router
from routers import edge as edge_router
from library import ensure_dirs as ensure_library_dirs, cleanup_pending
from automatic_tasks import (startup, clean_expired_directories, clear_expired_bills,
                             clean_printed_sources, clean_stale_pending,
                             clean_finished_groups, backup_database,
                             release_scheduled_jobs, expire_stale_scheduled)

ml_models = {}
logging.getLogger('apscheduler.executors.default').propagate = False


# lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 运行时目录在启动阶段创建，避免 import main 就产生文件系统副作用
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('pending_files', exist_ok=True)
    ensure_library_dirs()  # 共享资料库待审目录 / 公开目录
    db.init_db()  # 初始化数据库（打印订单/设备/告警三张表）
    startup()  # 启动清理（删除记录要写库，必须在 init_db 之后）
    # 启动时把数据库中未完成的订单恢复到打印队列，做到重启不丢订单
    print_queue_singleton().data.extend(db.load_open_jobs())
    # 只有抢到单实例锁的进程才跑定时任务：多副本部署时避免重复告警/重复清理
    scheduler = BackgroundScheduler()
    if instance_lock.acquire():
        scheduler.add_job(clean_expired_directories, 'interval', seconds=5, )  # auto remove expired directory
        scheduler.add_job(clear_expired_bills, 'interval', seconds=5, )
        scheduler.add_job(clean_printed_sources, 'interval', seconds=30, )  # 打印完成 5 分钟后删源文件
        scheduler.add_job(clean_stale_pending, 'interval', seconds=300, )  # 失败/完成残留副本 24 小时清理
        scheduler.add_job(clean_finished_groups, 'interval', seconds=300, )  # 已结算拼单目录清理
        scheduler.add_job(alerts.evaluate, 'interval', seconds=10, )  # 每 10 秒扫描一次告警规则
        # 每 60 秒兜底一次「卡在打印中」的订单：超过 30 分钟未完成标记为 failed
        scheduler.add_job(db.fail_stale_printing, 'interval', seconds=60, )
        # 运维 agent：每 20 秒跑一轮——物理故障（缺纸/卡纸/断电/门磁）只上报人工处理；
        # 在线设备上打印链路假死（卡在 printing 超 5 分钟）自动下发重启自愈
        scheduler.add_job(ops_agent.scan, 'interval', seconds=20, )
        # 定时打印：每 30 秒把到点的预约订单放行进入打印队列；
        # 每 5 分钟把到点后 12 小时仍未打印的预约单作废（避免设备长期离线隔天突然出纸）
        scheduler.add_job(release_scheduled_jobs, 'interval', seconds=30, )
        scheduler.add_job(expire_stale_scheduled, 'interval', seconds=300, )
        # 共享资料库：超过 7 天仍未审完的资料自动关闭并删待审文件
        scheduler.add_job(cleanup_pending, 'interval', hours=6, )
        # 每天凌晨 3:10 备份数据库，保留最近 7 份
        scheduler.add_job(backup_database, 'cron', hour=3, minute=10)
        scheduler.start()
        # 补发上次进程退出前「已落库但未推送成功」的告警
        notify.resend_unpushed_alerts()
    else:
        logging.warning("未获取到调度锁：本实例只提供 HTTP 服务，不运行定时任务")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    instance_lock.release()
    ml_models.clear()


app = FastAPI(lifespan=lifespan)  # 开启 /docs 与 /openapi.json，方便本地调试浏览接口
app.include_router(user.router)
app.include_router(pay.router)
app.include_router(group.router)
app.include_router(printer.router)
app.include_router(device.router)
app.include_router(alert.router)
app.include_router(admin.router)
app.include_router(team.router)
app.include_router(auth.router)
app.include_router(tenant.router)
app.include_router(library_router.router)
app.include_router(display_router.router)
app.include_router(edge_router.router)
app.add_middleware(
    CORSMiddleware,
    # 白名单来自 ALLOWED_ORIGINS；纯 Bearer 鉴权不使用 cookie，
    # 因此 allow_credentials=False，避免「* + credentials」的规范冲突与越权风险。
    allow_origins=setting.ORIGIN,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def negotiate_static_cache(request, call_next):
    """静态页面 / 脚本 / 样式一律走协商缓存（Cache-Control: no-cache）。

    no-cache 不是「不缓存」，而是每次都拿 ETag/Last-Modified 回来验一次：
    文件没变时 StaticFiles 直接回 304，几乎没有传输开销；文件变了立刻生效。
    否则部署或改代码后，浏览器会继续用旧页面/旧脚本（本项目的管理台与用户端
    都是静态文件，很容易踩到）。
    """
    response = await call_next(request)
    path = request.url.path
    ctype = response.headers.get("content-type", "")
    if (path.endswith((".html", ".css", ".js")) or path == "/"
            or path.endswith("/") or ctype.startswith("text/html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def security_headers(request, call_next):
    """统一补齐公网安全响应头（管理端/用户端/接口都生效）。

    CSP 保留 'unsafe-inline'：管理端页面与 Element Plus 都有内联脚本/样式；
    但仍限制脚本只能来自本站、禁止 object 与外部框架嵌入，能挡住常见的
    外链脚本注入与点击劫持。HSTS 只在 HTTPS 请求（含反代 X-Forwarded-Proto）
    上追加，避免本地 HTTP 调试被浏览器强制升级。
    """
    response = await call_next(request)
    for name, value in _security_headers_for(request).items():
        response.headers.setdefault(name, value)
    return response


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'"
    ),
}
HSTS_HEADER = "max-age=31536000; includeSubDomains"


def _security_headers_for(request):
    """返回本次响应要补的安全头；HTTPS 时额外加 HSTS。"""
    headers = dict(_SECURITY_HEADERS)
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if request.url.scheme == "https" or proto == "https":
        headers["Strict-Transport-Security"] = HSTS_HEADER
    return headers


@app.get("/healthz", include_in_schema=False)
def healthz():
    """健康检查：数据库可读 + 本实例是否持有调度锁。"""
    db_ok = True
    try:
        conn = db._connect()
        conn.execute("SELECT 1")
        conn.close()
    except Exception:
        db_ok = False
    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "scheduler": instance_lock.held(),
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@app.get("/api/info")
async def api_info():
    """接口清单（原根路由的 JSON，挪到 /api/info）。"""
    return {
        "service": "思印客 Silnk 后端",
        "status": "running",
        "docs": "/docs",
        "ui": "/ui",
        "tenant_ui": "/tenant",
        "user": "/",
        "endpoints": [
            "/api/device/register", "/api/device/heartbeat",
            "/api/device/list", "/api/device/{device_id}",
            "/api/device/{device_id} (DELETE)",
            "/api/device/{device_id}/paper",
            "/api/alert/list", "/api/alert/{id}/resolve", "/api/alert/test-push",
            "/api/agent/runs", "/api/agent/scan",
            "/api/order/list",
            "/api/order/reprint/list",
            "/api/order/{order_id}/reprint/approve",
            "/api/order/{order_id}/reprint/reject",
            "/api/user/reprint/{order_id}",
            "/api/price", "/api/price/config", "/api/price/compare",
            "/api/price/devices", "/api/price/device/{device_id}",
            "/api/price/device/{device_id} (DELETE)",
            "/api/pay/info", "/api/pay/config", "/api/pay/list",
            "/api/pay/{payment_id}/confirm",
            "/api/status",
            "/api/team/tasks", "/api/team/tasks/{task_id}",
            "/api/printer/available",
            "/uploadfile", "/uploadfile/filelist", "/uploadfile/convert_status",
            "/pay/bill/create", "/pay/wechat/submit",
            "/api/group/create", "/api/group/{group_no}",
            "/api/group/join", "/api/group/leave/{group_no}",
            "/api/group/remove/{group_no}/{member_id}",
            "/api/group/settle/{group_no}", "/api/group/reopen/{group_no}",
            "/api/tenant/create", "/api/tenant/list",
            "/api/tenant/{tenant_id}", "/api/tenant/{tenant_id}/status",
            "/api/tenant/{tenant_id}/subscription", "/api/tenant/{tenant_id}/price",
            "/api/tenant/self", "/api/tenant/self/devices",
            "/api/tenant/self/orders", "/api/tenant/self/alerts",
            "/api/tenant/self/price",
        ],
    }


@app.get("/ui")
async def ui():
    """管理台入口：跳转到按功能拆分的多页管理台首页。"""
    return RedirectResponse(url="/admin/", status_code=307)


@app.get("/team")
async def team_entry():
    """团队协作看板入口：补尾斜杠，避免 StaticFiles 对 /team 直接 404。"""
    return RedirectResponse(url="/team/", status_code=307)


@app.get("/intro")
async def intro_redirect():
    return RedirectResponse(url="/intro/", status_code=307)


@app.get("/tenant")
async def tenant_entry():
    """租户控制台入口（软件订阅线）：补尾斜杠，避免 StaticFiles 对 /tenant 直接 404。"""
    return RedirectResponse(url="/tenant/", status_code=307)


# ---- 静态站点托管（与后端同源，免跨域）----
# 用户端：基座完整 Vue 前端的构建产物（static/app/dist），托管在根路径
_USER_DIST = os.path.join(os.path.dirname(__file__), "static", "app", "dist")
# 管理台：按功能拆分的多页站点（static/admin）
_ADMIN_DIR = os.path.join(os.path.dirname(__file__), "static", "admin")
# 收款码图片（个人微信收款码等）：单独放 static/pay，
# 不放进用户端构建产物 dist —— dist 每次 npm run build 会被清空。
_PAY_QR_DIR = os.path.join(os.path.dirname(__file__), "static", "pay")
# 团队协作看板（独立应用，入口 /team）
_TEAM_DIR = os.path.join(os.path.dirname(__file__), "static", "team")
# 租户控制台（软件订阅线：每个打印点客户用租户密钥登录，只看自己租户的数据）
_TENANT_DIR = os.path.join(os.path.dirname(__file__), "static", "tenant")
_LANDING_DIR = os.path.join(os.path.dirname(__file__), "static", "landing")
os.makedirs(_USER_DIST, exist_ok=True)
os.makedirs(_LANDING_DIR, exist_ok=True)
os.makedirs(_ADMIN_DIR, exist_ok=True)
os.makedirs(_PAY_QR_DIR, exist_ok=True)
os.makedirs(_TEAM_DIR, exist_ok=True)
os.makedirs(_TENANT_DIR, exist_ok=True)
# 注意挂载顺序：先具体路径（/admin、/pay-qr、/team、/tenant），再挂根路径，避免被根挂载吞掉


@app.get("/login", include_in_schema=False)
async def user_login_page():
    """SPA 深链接兜底：直接打开 /login 时返回 index.html，交给前端路由。"""
    return FileResponse(os.path.join(_USER_DIST, "index.html"))


@app.get("/register", include_in_schema=False)
async def user_register_page():
    """SPA 深链接兜底：直接打开 /register 时返回 index.html，交给前端路由。"""
    return FileResponse(os.path.join(_USER_DIST, "index.html"))


@app.get("/library", include_in_schema=False)
async def user_library_page():
    """SPA 深链接兜底：直接打开 /library 时返回 index.html，交给前端路由。"""
    return FileResponse(os.path.join(_USER_DIST, "index.html"))


@app.get("/display", include_in_schema=False)
async def display_terminal_page():
    """取件屏大屏兜底页（L1 降级档）。

    显式路由而非放在 static/ 下：根路径被 Vue 的 static/app/dist 挂载占着，
    /display.html 会被根挂载吞掉返回 404。这里显式返回文件。
    """
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "display.html"))


app.mount("/admin", StaticFiles(directory=_ADMIN_DIR, html=True), name="admin")
app.mount("/pay-qr", StaticFiles(directory=_PAY_QR_DIR), name="pay-qr")
app.mount("/team", StaticFiles(directory=_TEAM_DIR, html=True), name="team")
app.mount("/tenant", StaticFiles(directory=_TENANT_DIR, html=True), name="tenant")
app.mount("/intro", StaticFiles(directory=_LANDING_DIR, html=True), name="landing")
app.mount("/", StaticFiles(directory=_USER_DIST, html=True), name="user-front")


logging.getLogger("uvicorn.access").addFilter(GetJobsAccessLogFilter())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
