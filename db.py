"""数据库模块。

仅使用 Python 标准库 sqlite3（不引入 SQLAlchemy 等任何第三方依赖），
负责持久化打印订单、设备与告警三类数据。
数据库文件为仓库根目录下的 printer.db。

线程安全说明：后台定时任务（APScheduler）会并发调用本模块，
因此每次调用都新建连接（check_same_thread=False），用完即关。
"""

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

import setting

# 数据库文件固定放在仓库根目录
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "printer.db")


def _connect():
    """新建一个 sqlite 连接，允许跨线程使用，结果按字典方式读取。

    开启 WAL 与 busy_timeout：后台有心跳、告警、打印状态多个线程同时写，
    默认的 rollback journal 容易报 database is locked。
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.DatabaseError:
        pass
    return conn


def _now():
    """统一的时间戳字符串格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value):
    """把可能为字符串/None 的值安全地转成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    """把可能为字符串/None 的值安全地转成 float（用于价格配置）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool_int(value):
    """把布尔值转成 0/1 存入 sqlite（None 表示未上报）。"""
    if value is None:
        return None
    return 1 if value else 0


def init_db():
    """建表（幂等），首次启动时调用。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        # 打印订单表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS print_jobs (
                id INTEGER PRIMARY KEY,
                folder TEXT,
                filename TEXT,
                print_copies INTEGER,
                print_range_start INTEGER,
                print_range_end INTEGER,
                print_side TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                printer_id TEXT,
                user_id INTEGER,
                amount REAL,
                out_trade_no TEXT,
                paper TEXT,
                print_color TEXT,
                batch_names TEXT,
                batch_mode TEXT,
                batch_owner TEXT,
                batch_pages INTEGER,
                batch_copies INTEGER,
                display_name TEXT,
                pickup_code TEXT,
                saved_amount REAL,
                eta_seconds INTEGER,
                eta_deadline TEXT,
                overtime INTEGER DEFAULT 0,
                waived INTEGER DEFAULT 0,
                waive_reason TEXT,
                reprint_of INTEGER,
                reprint_reason TEXT,
                reprint_status TEXT,
                progress_copies INTEGER,
                progress_pages INTEGER,
                progress_at TEXT,
                failure_alerted INTEGER DEFAULT 0,
                scheduled_at TEXT,
                picked_at TEXT,
                pickup_seq INTEGER,
                pickup_skipped INTEGER
            )
            """
        )
        # 文件删除记录：15 分钟过期、打印完成后 5 分钟清理、失败残留清理都留痕，
        # 管理端「接入状态」页可查，做到「删除记录可查」。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS file_deletions (
                id INTEGER PRIMARY KEY,
                folder TEXT,
                filename TEXT,
                order_no TEXT,
                reason TEXT,
                deleted_at TEXT
            )
            """
        )
        # 用户账号：手机号注册登录；status = active / banned（封禁后立即失效）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                phone TEXT UNIQUE NOT NULL,
                nickname TEXT,
                password_hash TEXT,
                status TEXT DEFAULT 'active',
                ban_reason TEXT,
                created_at TEXT,
                last_login_at TEXT
            )
            """
        )
        # 短信验证码：只存哈希，5 分钟有效，限次防爆破
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_codes (
                id INTEGER PRIMARY KEY,
                phone TEXT,
                code_hash TEXT,
                expires_at REAL,
                attempts INTEGER DEFAULT 0,
                used INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        # 未支付账单：落库保存，服务重启或多实例时都能读到（JSON 存完整账单）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bills (
                out_trade_no TEXT PRIMARY KEY,
                payload TEXT,
                expiry REAL,
                created_at TEXT
            )
            """
        )
        # 设备表
        # 除任务规定字段外，额外保存心跳上报的传感器字段
        # （paper_left / ups_online / door_open / pickup_seq），
        # 供告警引擎 evaluate() 使用。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY,
                device_id TEXT UNIQUE,
                name TEXT,
                model TEXT,
                driver TEXT,
                conn TEXT,
                status TEXT,
                paper_capacity INTEGER,
                pages_printed INTEGER,
                last_heartbeat TEXT,
                created_at TEXT,
                paper_left INTEGER,
                ups_online INTEGER,
                door_open INTEGER,
                pickup_seq INTEGER,
                paper_state TEXT,
                paper_baseline_pages INTEGER,
                ups_runtime_min INTEGER,
                store_name TEXT,
                address TEXT,
                business_hours TEXT,
                lat REAL,
                lng REAL,
                supports_color INTEGER,
                supports_duplex INTEGER,
                speed_ppm REAL,
                printers TEXT
            )
            """
        )
        # 告警表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY,
                rule TEXT,
                device_id TEXT,
                level TEXT,
                message TEXT,
                triggered_at TEXT,
                resolved_at TEXT,
                push_status TEXT DEFAULT 'pending',
                pushed_at TEXT,
                push_attempts INTEGER DEFAULT 0,
                push_detail TEXT
            )
            """
        )
        # 价格配置表（单行，id 固定为 1）：管理端可改打印单价，
        # 用户端显示与后端结算共用同一份配置，避免前后端价格不一致。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS price_config (
                id INTEGER PRIMARY KEY,
                bw_price REAL,
                color_price REAL,
                bw_duplex_price REAL,
                color_duplex_price REAL,
                updated_at TEXT
            )
            """
        )
        # 单台设备的独立定价：有记录时该设备用自己的价，没记录就跟随全局 price_config。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_price (
                device_id TEXT PRIMARY KEY,
                bw_price REAL,
                color_price REAL,
                bw_duplex_price REAL,
                color_duplex_price REAL,
                updated_at TEXT
            )
            """
        )
        # 收款配置（单行，id 固定为 1）：当前用个人微信收款码收款，
        # 收款码图片路径与收款人昵称都可改，改完用户端下单弹窗立即生效。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pay_config (
                id INTEGER PRIMARY KEY,
                wechat_qr TEXT,
                payee TEXT,
                updated_at TEXT
            )
            """
        )
        # 收款流水：每一笔「用户声明已付 + 管理端核验」的账单都留痕，
        # 管理端「账单流水」页据此看到收了多少钱。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY,
                out_trade_no TEXT UNIQUE,
                device_id TEXT,
                amount REAL,
                method TEXT,
                status TEXT,
                detail TEXT,
                pages INTEGER,
                sheets INTEGER,
                payer_note TEXT,
                created_at TEXT,
                confirmed_at TEXT
            )
            """
        )
        # 团队协作任务表（独立看板 /team 使用）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                title TEXT,
                team TEXT,
                status TEXT,
                priority TEXT,
                progress INTEGER,
                assignee TEXT,
                tags TEXT,
                note TEXT,
                starred INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        # 设备指令：管理端下发「重启打印机端」等指令，客户端轮询执行并回报结果
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_commands (
                id INTEGER PRIMARY KEY,
                device_id TEXT,
                command TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                updated_at TEXT,
                result TEXT
            )
            """
        )
        # 设备日志：客户端把最近的运行日志推上来，管理端设备详情页实时查看
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_logs (
                device_id TEXT PRIMARY KEY,
                content TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eject_events (
                id INTEGER PRIMARY KEY,
                device_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event TEXT NOT NULL,
                source TEXT NOT NULL,
                ts INTEGER NOT NULL,
                confidence REAL,
                created_at TEXT NOT NULL,
                order_id INTEGER,
                UNIQUE(device_id, event_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_eject_events_device_ts"
            " ON eject_events(device_id, ts DESC)"
        )
        # 出纸现场图：X5 识别到出纸时抓的那一帧，供管理端核对是否真的出纸。
        # 单独放一张表，避免给 eject_events 加 BLOB 导致事件查询变慢。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eject_photos (
                device_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                image BLOB NOT NULL,
                mime TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (device_id, event_id)
            )
            """
        )
        # 零配置接入：工控机首次启动时申请配对，管理端批准后下发设备令牌。
        # code 是给人核对的短码，secret_hash 用来防止别人抢走这次配对；
        # token_plain 只在批准后暂存一次，客户端取走后立即清空。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_claims (
                claim_id TEXT PRIMARY KEY,
                code TEXT,
                secret_hash TEXT,
                hostname TEXT,
                fingerprint TEXT,
                model TEXT,
                selected_printer TEXT,
                printers TEXT,
                paper_capacity INTEGER,
                status TEXT DEFAULT 'pending',
                device_id TEXT,
                token_plain TEXT,
                created_at INTEGER,
                expires_at INTEGER,
                bound_at INTEGER
            )
            """
        )
        # 每台设备一个长期令牌：只存哈希，管理端可吊销。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_tokens (
                device_id TEXT PRIMARY KEY,
                token_hash TEXT,
                created_at INTEGER,
                revoked INTEGER DEFAULT 0
            )
            """
        )
        # 运维 agent 处理记录：每轮扫描对一台设备/一条告警的处置留痕。
        # 物理故障（缺纸/门磁/断电/取件顺序）只上报，等人工到现场；
        # 打印链路假死（设备在线但订单卡在 printing）才自动下发重启自愈。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY,
                kind TEXT,
                device_id TEXT,
                alert_id INTEGER,
                job_folder TEXT,
                job_filename TEXT,
                verdict TEXT,
                reason TEXT,
                action TEXT,
                action_status TEXT,
                command_id INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        # 拼单打印：一个人发起，链接/二维码发到宿舍群、课程群，同学各自扫码上传；
        # 人越多折扣越低，发起人统一付款、统一取件（每人一份带姓名封皮）。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS print_groups (
                id INTEGER PRIMARY KEY,
                group_no TEXT UNIQUE,
                owner_name TEXT,
                owner_key TEXT,
                owner_uid INTEGER,
                device_id TEXT,
                status TEXT DEFAULT 'open',
                discount REAL,
                total_price REAL,
                order_no TEXT,
                pickup_code TEXT,
                pay_mode TEXT,
                created_at TEXT,
                settled_at TEXT
            )
            """
        )
        # 拼单成员：每个人自己的文件与打印参数。文件在加入时已复制到
        # uploads/group_{group_no}/，不依赖各自的会话目录，避免 15 分钟过期丢文件。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS group_members (
                id INTEGER PRIMARY KEY,
                group_no TEXT,
                name TEXT,
                folder TEXT,
                filename TEXT,
                print_copies INTEGER,
                print_range_start INTEGER,
                print_range_end INTEGER,
                print_side TEXT,
                print_color TEXT,
                paper TEXT,
                skip_blank INTEGER,
                pages INTEGER,
                amount REAL,
                saved_amount REAL,
                blank_pages TEXT,
                user_id INTEGER,
                payable REAL,
                paid INTEGER DEFAULT 0,
                out_trade_no TEXT,
                paid_at TEXT,
                pickup_code TEXT,
                created_at TEXT
            )
            """
        )
        # 印前合规审核报告：GLM 视觉模型逐页审核结果，供用户端提示与管理端留档
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS compliance_reports (
                id INTEGER PRIMARY KEY,
                filename TEXT,
                verdict TEXT,
                categories TEXT,
                reason TEXT,
                model TEXT,
                pages INTEGER,
                page_count INTEGER,
                detail TEXT,
                created_at TEXT
            )
            """
        )
        # 租户（软件订阅线）：一个租户 = 一个客户（打印点 / 学校文印室）。
        # tenant_id 是租户的业务标识（slug），secret_key 是该租户自己的管理密钥。
        # 平台方用全局 SECRET_KEY 管理所有租户；租户用自己的密钥只看自己租户的数据。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY,
                tenant_id TEXT UNIQUE,
                name TEXT,
                secret_key TEXT,
                status TEXT DEFAULT 'active',
                contact TEXT,
                note TEXT,
                created_at TEXT
            )
            """
        )
        # 订阅表：一行一个租户的当前订阅（手动开通 / 手动续期，暂不自动计费）。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                tenant_id TEXT PRIMARY KEY,
                plan TEXT,
                printer_limit INTEGER,
                status TEXT DEFAULT 'active',
                started_at TEXT,
                expires_at TEXT,
                note TEXT,
                updated_at TEXT,
                fee_rate REAL DEFAULT 0.1
            )
            """
        )
        # 租户级默认单价：该租户下所有机器的默认价。设了就覆盖平台全局价；
        # 单台机器另有 device_price 时以设备价优先（设备级 > 租户级 > 平台全局级）。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_price (
                tenant_id TEXT PRIMARY KEY,
                bw_price REAL,
                color_price REAL,
                bw_duplex_price REAL,
                color_duplex_price REAL,
                updated_at TEXT
            )
            """
        )
        # 结算记录：平台方按租户手动结算打印流水，一行一次结算。
        # 订单是否已结算用 print_jobs.settled 标记，避免同一单重复结算。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_settlements (
                id INTEGER PRIMARY KEY,
                tenant_id TEXT,
                orders INTEGER,
                amount REAL,
                fee_rate REAL,
                fee REAL,
                net REAL,
                period_start TEXT,
                period_end TEXT,
                settled_at TEXT,
                operator TEXT,
                note TEXT
            )
            """
        )
        # 共享资料库：用户自愿共享 → agent 初审 → 商家管理员复审 → 公开。
        # pending_path 是待审文件，published_path 是公开文件；两者都只存服务端路径。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_docs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                category TEXT,
                pages INTEGER,
                size_bytes INTEGER,
                status TEXT DEFAULT 'pending_agent',
                agent_verdict TEXT,
                agent_reason TEXT,
                agent_model TEXT,
                agent_at TEXT,
                admin_note TEXT,
                reviewer TEXT,
                reviewed_at TEXT,
                uploader_uid INTEGER,
                uploader_name TEXT,
                tenant_id TEXT DEFAULT 'default',
                order_no TEXT,
                pending_path TEXT,
                published_path TEXT,
                filename TEXT,
                downloads INTEGER DEFAULT 0,
                print_count INTEGER DEFAULT 0,
                print_pages INTEGER DEFAULT 0,
                created_at TEXT,
                published_at TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_status"
            " ON library_docs(status, created_at)"
        )
        # 资料库分类：可自定义，后台增删；默认分类仅在表为空时预置
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_categories (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        for order, name in enumerate(
                ("论文", "课件", "习题", "文学", "法规", "通识", "简历", "其他")):
            cur.execute(
                "INSERT OR IGNORE INTO library_categories"
                " (name, sort_order, created_at) VALUES (?, ?, ?)",
                (name, order, _now()))
        # 轻量迁移：老库可能缺少新增列，用 PRAGMA 检查并补列，保证平滑升级
        _ensure_columns(cur)
        # 多租户迁移：确保默认租户存在，并把历史数据（设备/订单/告警）归到默认租户。
        # 默认租户密钥沿用全局 SECRET_KEY，保证原有单租户部署升级后行为不变。
        _seed_default_tenant(cur)
        conn.commit()
    finally:
        conn.close()
    # 首次启动时预填真实功能清单（表为空才写，幂等）
    seed_tasks()


def _seed_default_tenant(cur):
    """保证默认租户存在并把历史数据归入默认租户（幂等）。

    默认租户 tenant_id = 'default'，密钥 = 全局 SECRET_KEY：
    升级前的老数据没有 tenant_id，这里统一回填为 'default'，
    因此原有单租户部署升级后看到的仍是同一批设备/订单/告警。
    """
    cur.execute(
        "INSERT OR IGNORE INTO tenants"
        " (tenant_id, name, secret_key, status, contact, note, created_at)"
        " VALUES ('default', '平台默认租户', ?, 'active', '', '', ?)",
        (setting.SECRET_KEY, _now()),
    )
    cur.execute(
        "INSERT OR IGNORE INTO subscriptions"
        " (tenant_id, plan, printer_limit, status, started_at, expires_at,"
        " note, updated_at, fee_rate)"
        " VALUES ('default', 'platform', 0, 'active', ?, NULL,"
        " '平台默认租户，不设打印机上限', ?, 0)",
        (_now(), _now()),
    )
    # 平台自营的默认租户不抽成，避免「平台给自己结算」的假流水
    cur.execute("UPDATE subscriptions SET fee_rate=0 WHERE tenant_id='default'")

    for table in ("devices", "print_jobs", "alerts"):
        cur.execute(f"UPDATE {table} SET tenant_id='default' WHERE tenant_id IS NULL")


def _ensure_columns(cur):
    """检查并补齐新增列（幂等，可重复调用）。

    已存在的 printer.db 可能是旧结构（无 printer_id / paper_state），
    这里用 PRAGMA table_info 判断缺列后执行 ALTER TABLE ADD COLUMN。
    """
    for table, columns in (
        ("eject_events", (("order_id", "INTEGER"),)),
        ("print_jobs", (("printer_id", "TEXT"), ("user_id", "INTEGER"),
                        ("amount", "REAL"),
                        ("out_trade_no", "TEXT"), ("paper", "TEXT"),
                        ("print_color", "TEXT"), ("status_at", "TEXT"),
                        ("batch_names", "TEXT"), ("batch_mode", "TEXT"),
                        ("batch_owner", "TEXT"), ("batch_pages", "INTEGER"),
                        ("batch_copies", "INTEGER"), ("display_name", "TEXT"),
                        ("pickup_code", "TEXT"), ("saved_amount", "REAL"),
                        ("eta_seconds", "INTEGER"), ("eta_deadline", "TEXT"),
                        ("overtime", "INTEGER"), ("waived", "INTEGER"),
                        ("waive_reason", "TEXT"), ("reprint_of", "INTEGER"),
                        ("reprint_reason", "TEXT"), ("reprint_status", "TEXT"),
                        ("progress_copies", "INTEGER"), ("progress_pages", "INTEGER"),
                        ("progress_at", "TEXT"), ("failure_alerted", "INTEGER"),
                        ("tenant_id", "TEXT"),
                        # 定时打印：预约出纸时间（为空表示立即打印）
                        ("scheduled_at", "TEXT"),
                        # SaaS 结算：该订单是否已由平台方结算给租户
                        ("settled", "INTEGER"),
                        # 取件确认：取走时间、取件序号（从 1 开始）、
                        # 本次取件跳过了前面几单（>0 即乱序，供 R5 告警留痕）
                        ("picked_at", "TEXT"), ("pickup_seq", "INTEGER"),
                        ("pickup_skipped", "INTEGER"))),
        ("devices", (
            ("paper_state", "TEXT"),
            ("paper_baseline_pages", "INTEGER"),
            ("ups_runtime_min", "INTEGER"),
            ("store_name", "TEXT"),
            ("address", "TEXT"),
            ("business_hours", "TEXT"),
            ("lat", "REAL"),
            ("lng", "REAL"),
            ("supports_color", "INTEGER"),
            ("supports_duplex", "INTEGER"),
            ("speed_ppm", "REAL"),
            ("printers", "TEXT"),
            ("tenant_id", "TEXT"),
        )),
        ("price_config", (("bw_price", "REAL"), ("color_price", "REAL"),
                          ("bw_duplex_price", "REAL"), ("color_duplex_price", "REAL"))),
        # 账号密码登录：PBKDF2 哈希，老账号为空表示尚未设置
        ("users", (("password_hash", "TEXT"),)),
        ("alerts", (("push_status", "TEXT"), ("pushed_at", "TEXT"),
                    ("push_attempts", "INTEGER"), ("push_detail", "TEXT"),
                    ("tenant_id", "TEXT"))),
        ("device_price", (("bw_price", "REAL"), ("color_price", "REAL"),
                          ("bw_duplex_price", "REAL"), ("color_duplex_price", "REAL"))),
        # 共享资料库：打印量统计（付费下单时累计）
        ("library_docs", (("print_count", "INTEGER"), ("print_pages", "INTEGER"))),
        # SaaS：每租户抽成比例（0–1），老库升级时补列
        ("subscriptions", (("fee_rate", "REAL"),)),
        # 配对：记录操作员在工控机上选中的打印队列，批准时按它写能力
        ("device_claims", (("selected_printer", "TEXT"),)),
        # 拼单 AA：付款方式与每个成员的应付/已付/取件信息
        ("print_groups", (("pay_mode", "TEXT"), ("owner_uid", "INTEGER"))),
        ("group_members", (("user_id", "INTEGER"), ("payable", "REAL"),
                           ("paid", "INTEGER"), ("out_trade_no", "TEXT"),
                           ("paid_at", "TEXT"), ("pickup_code", "TEXT"))),
    ):
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        for name, col_type in columns:
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


# ----------------------- 打印订单 print_jobs -----------------------

def add_job(job, printer_id=None):
    """新增一条打印订单，返回新记录 id。

    job 为包含 folder/filename/print_copies/... 的字典，
    与打印队列中的元素结构保持一致。
    订单按目标设备归属的租户打标（设备无租户 / 未指定设备时归 default）。
    """
    owner_id = printer_id if printer_id is not None else job.get("printer_id")
    tenant_id = "default"
    if owner_id:
        device = get_device(owner_id)
        if device and device.get("tenant_id"):
            tenant_id = device["tenant_id"]
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO print_jobs
                (folder, filename, print_copies, print_range_start,
                 print_range_end, print_side, status, created_at, printer_id,
                 user_id,
                 amount, out_trade_no, paper, print_color, status_at,
                 batch_names, batch_mode, batch_owner, batch_pages, batch_copies,
                 display_name, pickup_code, saved_amount,
                 eta_seconds, eta_deadline, overtime, waived, waive_reason,
                 reprint_of, reprint_reason, reprint_status, tenant_id, scheduled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.get("folder"),
                job.get("filename"),
                _to_int(job.get("print_copies")),
                _to_int(job.get("print_range_start")),
                _to_int(job.get("print_range_end")),
                job.get("print_side"),
                job.get("status") or "pending",
                _now(),
                # 显式传参优先，否则取 job 内自带的 printer_id（旧订单可能为空）
                owner_id,
                # 下单账号：登录后历史订单按 user_id 跨设备查询
                _to_int(job.get("user_id")),
                # 本文件的实付金额与所属支付单号（用于管理端订单金额列）
                _to_float(job.get("amount")),
                job.get("out_trade_no"),
                job.get("paper") or "A4",
                job.get("print_color") or "bw",
                _now(),
                # 班级/社团批量：名单、取件方式、组织者
                json.dumps((job.get("batch") or {}).get("names") or [],
                           ensure_ascii=False)
                if job.get("batch") else None,
                (job.get("batch") or {}).get("mode"),
                (job.get("batch") or {}).get("owner"),
                # 批量：每份正文页数与原始份数（print_copies 已改成 1，用于整包打印）
                _to_int((job.get("batch") or {}).get("content_pages")),
                _to_int((job.get("batch") or {}).get("copies")),
                # 真实文件名只留在服务端，打印机端只看得到订单号文件名
                job.get("display_name") or job.get("filename"),
                job.get("pickup_code"),
                _to_float(job.get("saved_amount")),
                # 交付承诺：预计耗时与承诺完成时间（超时免单判定基线）
                _to_int(job.get("eta_seconds")),
                job.get("eta_deadline"),
                _to_int(job.get("overtime")) or 0,
                _to_int(job.get("waived")) or 0,
                job.get("waive_reason"),
                # 免费重印：本条是原单 order_id 的重印件
                _to_int(job.get("reprint_of")),
                job.get("reprint_reason"),
                None,
                tenant_id,
                # 定时打印：预约出纸时间（空 = 立即打印）
                job.get("scheduled_at"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row_to_job(row):
    """把数据库行转成打印队列可用的字典。"""
    return {
        "id": row["id"],
        "folder": row["folder"],
        "filename": row["filename"],
        "print_copies": row["print_copies"],
        "print_range_start": row["print_range_start"],
        "print_range_end": row["print_range_end"],
        "print_side": row["print_side"],
        "status": row["status"],
        "created_at": row["created_at"],
        "printer_id": row["printer_id"],
        "user_id": _to_int(row["user_id"] if "user_id" in row.keys() else None),
        "amount": _to_float(row["amount"]) if "amount" in row.keys() else None,
        "out_trade_no": row["out_trade_no"] if "out_trade_no" in row.keys() else None,
        "paper": (row["paper"] if "paper" in row.keys() else None) or "A4",
        "print_color": (row["print_color"] if "print_color" in row.keys() else None) or "bw",
        "status_at": row["status_at"] if "status_at" in row.keys() else None,
        "batch_names": _load_batch_names(
            row["batch_names"] if "batch_names" in row.keys() else None),
        "batch_mode": row["batch_mode"] if "batch_mode" in row.keys() else None,
        "batch_owner": row["batch_owner"] if "batch_owner" in row.keys() else None,
        "batch_pages": _to_int(
            row["batch_pages"] if "batch_pages" in row.keys() else None),
        "batch_copies": _to_int(
            row["batch_copies"] if "batch_copies" in row.keys() else None),
        "display_name": (row["display_name"] if "display_name" in row.keys() else None),
        "pickup_code": (row["pickup_code"] if "pickup_code" in row.keys() else None),
        "saved_amount": _to_float(
            row["saved_amount"] if "saved_amount" in row.keys() else None),
        # 交付承诺：预计耗时、承诺完成时间、是否超时免单
        "eta_seconds": _to_int(
            row["eta_seconds"] if "eta_seconds" in row.keys() else None),
        "eta_deadline": (row["eta_deadline"] if "eta_deadline" in row.keys() else None),
        "overtime": _to_int(row["overtime"] if "overtime" in row.keys() else None),
        "waived": _to_int(row["waived"] if "waived" in row.keys() else None),
        "waive_reason": (row["waive_reason"] if "waive_reason" in row.keys() else None),
        # 免费重印：本单来自哪张原单、申请原因、处理状态
        "reprint_of": _to_int(
            row["reprint_of"] if "reprint_of" in row.keys() else None),
        "reprint_reason": (row["reprint_reason"] if "reprint_reason" in row.keys() else None),
        "reprint_status": (row["reprint_status"] if "reprint_status" in row.keys() else None),
        # 打印进度：已提交份数 / 已打印页数（客户端上报，未上报时为 None）
        "progress_copies": _to_int(
            row["progress_copies"] if "progress_copies" in row.keys() else None),
        "progress_pages": _to_int(
            row["progress_pages"] if "progress_pages" in row.keys() else None),
        "progress_at": (row["progress_at"] if "progress_at" in row.keys() else None),
        "failure_alerted": _to_int(
            row["failure_alerted"] if "failure_alerted" in row.keys() else None) or 0,
        # 租户：订单归属的租户标识（老订单迁移后为 default）
        "tenant_id": (row["tenant_id"] if "tenant_id" in row.keys() else None),
        # 定时打印：预约出纸时间；status 为 scheduled 表示尚未到点、还没进打印队列
        "scheduled_at": (row["scheduled_at"] if "scheduled_at" in row.keys() else None),
        # 取件确认：取走时间、取件序号、本次跳过的待取订单数（乱序留痕）
        "picked_at": (row["picked_at"] if "picked_at" in row.keys() else None),
        "pickup_seq": _to_int(
            row["pickup_seq"] if "pickup_seq" in row.keys() else None),
        "pickup_skipped": _to_int(
            row["pickup_skipped"] if "pickup_skipped" in row.keys() else None),
    }


def _load_batch_names(raw):
    """批量名单以 JSON 存在 TEXT 列里；坏数据返回空列表，不抛异常。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def list_jobs_by_folder(folder):
    """按会话目录列出订单（用户端「最近订单」用），最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE folder=? ORDER BY id DESC", (folder,)
        )
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_job_status(folder, filename, status):
    """更新订单状态（printing / completed / failed）。

    打印机端先上报 printing（已下载、正在打），打印成功再报 completed；
    这样用户端才能真实显示「打印中」而不是下载即完成。
    status_at 记录状态变更时间，供「卡在打印中」的超时兜底使用。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_jobs SET status=?, status_at=? WHERE folder=? AND filename=?",
            (status, _now(), folder, filename),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def fail_stale_printing(seconds=1800):
    """把卡在 printing 超过 30 分钟的订单标记为 failed。

    客户端打印完成但状态上报失败、或进程中途被杀时，订单会永远停在「打印中」，
    用户端一直显示打印中。这里做超时兜底；30 分钟阈值避免误伤大文件。
    只处理有 status_at 的新订单，老数据不参与。
    """
    cutoff = (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_jobs SET status='failed', status_at=?"
            " WHERE status='printing' AND status_at IS NOT NULL AND status_at<?",
            (_now(), cutoff),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def assign_job_printer(folder, filename, printer_id):
    """把「未指定设备」的订单绑定到第一台来取任务的打印机。

    多台打印机同时运行时，未指定设备的订单不能谁取都打，
    否则两台机器会各打一份；由第一台取任务的设备认领后，其他设备就取不到了。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_jobs SET printer_id=?"
            " WHERE folder=? AND filename=? AND (printer_id IS NULL OR printer_id='')",
            (printer_id, folder, filename),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_job(folder, filename):
    """按 folder + filename 查询单条订单（最近一条），不存在返回 None。

    返回字段含 printer_id / print_range_start / print_range_end /
    print_copies / status，供完成订单时累计打印页数使用。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM print_jobs
            WHERE folder=? AND filename=?
            ORDER BY id DESC LIMIT 1
            """,
            (folder, filename),
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None
    finally:
        conn.close()


def get_edge_activity(device_id, now=None, completed_window=120):
    current = datetime.now() if now is None else now
    if not isinstance(current, datetime):
        raise ValueError("now 必须为 datetime")
    cutoff = current - timedelta(seconds=completed_window)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE printer_id=? "
            "AND status IN ('printing', 'completed') "
            "AND status_at IS NOT NULL ORDER BY id DESC",
            (device_id,))
        for row in cur.fetchall():
            job = _row_to_job(row)
            try:
                status_at = datetime.strptime(job["status_at"], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            if status_at > current:
                continue
            if job["status"] == "printing":
                if status_at >= current - timedelta(seconds=1800):
                    return {"active": True, "status": "printing", "order_id": job["id"],
                            "window_until": None}
            elif status_at >= cutoff:
                return {"active": True, "status": "completed", "order_id": job["id"],
                        "window_until": (status_at + timedelta(seconds=completed_window)).strftime(
                            "%Y-%m-%d %H:%M:%S")}
        return {"active": False, "status": None, "order_id": None, "window_until": None}
    finally:
        conn.close()


def confirm_pickup(device_id, code, window_seconds=300):
    """取件确认：按取件码把该设备上一张已完成订单标记为已取走。

    - 只认本设备、status=completed、取件码一致的订单；优先取最早一张未取走的。
    - 幂等：同一取件码已全部取走时不再写库，返回 duplicate=True 和第一次的结果。
    - pickup_seq 为本设备已取走序号 +1；pickup_skipped 为本次取走时，
      窗口期内排在它前面、仍未取走的订单数（>0 即乱序取件）。
    - 订单 status 保持 completed（统计/告警/用户端都以它为"已出纸"口径），
      取走与否由 picked_at 表示。
    返回 dict；找不到对应订单返回 None。
    """
    code = str(code or "").strip()
    if not device_id or not code:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "SELECT * FROM print_jobs WHERE printer_id=? AND status='completed'"
            " AND pickup_code=? ORDER BY (picked_at IS NOT NULL) ASC, id ASC LIMIT 1",
            (device_id, code))
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        if row["picked_at"]:
            conn.rollback()
            return {"order_id": row["id"], "duplicate": True,
                    "pickup_seq": _to_int(row["pickup_seq"]),
                    "skipped": _to_int(row["pickup_skipped"]) or 0,
                    "picked_at": row["picked_at"]}
        cutoff = (datetime.now() - timedelta(seconds=window_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S")
        cur.execute(
            "SELECT COUNT(*) AS n FROM print_jobs WHERE printer_id=?"
            " AND status='completed' AND picked_at IS NULL AND id<?"
            " AND status_at>=?",
            (device_id, row["id"], cutoff))
        skipped = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COALESCE(MAX(pickup_seq), 0) + 1 AS seq FROM print_jobs"
            " WHERE printer_id=?", (device_id,))
        seq = int(cur.fetchone()["seq"])
        stamp = _now()
        cur.execute(
            "UPDATE print_jobs SET picked_at=?, pickup_seq=?, pickup_skipped=?"
            " WHERE id=? AND picked_at IS NULL",
            (stamp, seq, skipped, row["id"]))
        if cur.rowcount == 0:
            conn.rollback()
            return None
        conn.commit()
        return {"order_id": row["id"], "duplicate": False, "pickup_seq": seq,
                "skipped": skipped, "picked_at": stamp}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recent_skipped_pickups(device_id, within_seconds=600):
    """近 within_seconds 秒内发生过乱序取件（pickup_skipped>0）的订单，供 R5 告警。"""
    cutoff = (datetime.now() - timedelta(seconds=within_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE printer_id=? AND pickup_skipped>0"
            " AND picked_at>=? ORDER BY id ASC", (device_id, cutoff))
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def list_jobs(tenant_id=None):
    """返回订单（含状态），按创建顺序排列；传 tenant_id 时只返回该租户的订单。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        if tenant_id:
            cur.execute("SELECT * FROM print_jobs WHERE tenant_id=? ORDER BY id ASC",
                        (tenant_id,))
        else:
            cur.execute("SELECT * FROM print_jobs ORDER BY id ASC")
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def add_file_deletion(folder, filename, order_no, reason):
    """记录一次文件删除（过期/打印完成/失败残留），供管理端审计。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO file_deletions (folder, filename, order_no, reason, deleted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (folder, filename, order_no, reason, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_file_deletions(limit=200):
    """最近的删除记录，最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM file_deletions ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def folder_jobs_summary():
    """按会话目录汇总订单状态，供「打印完成后清理源文件」判断。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT folder,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('completed', 'failed') THEN 1 ELSE 0 END)
                       AS finished,
                   MAX(CASE WHEN status = 'completed' THEN status_at END) AS last_completed,
                   MAX(out_trade_no) AS order_no
            FROM print_jobs
            GROUP BY folder
            """
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def complete_job(job_id=None, folder=None, filename=None):
    """把订单标记为已完成。

    优先按 job_id 定位；若未给出 job_id，则按 folder + filename 匹配所有未完成记录。
    返回被更新的记录数。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        if job_id is not None:
            cur.execute(
                "UPDATE print_jobs SET status='completed', status_at=? WHERE id=?",
                (_now(), job_id),
            )
        else:
            cur.execute(
                """
                UPDATE print_jobs SET status='completed', status_at=?
                WHERE folder=? AND filename=? AND status!='completed'
                """,
                (_now(), folder, filename),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ----------------------- 交付承诺：超时免单 + 免费重印 -----------------------

def get_job_by_id(order_id):
    """按 id 查询单条订单，不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM print_jobs WHERE id=?", (order_id,))
        row = cur.fetchone()
        return _row_to_job(row) if row else None
    finally:
        conn.close()


def waive_payment(out_trade_no):
    """超时免单：把对应收款流水改为「免收 / 待退款」。

    - submitted（用户声明已付、待核验）→ waived（免收，不再确认收款）
    - confirmed（管理端已核验收到款）→ refund_pending（待退款，线下人工处理）
    个人收款码没有自动退款通道，这里如实标注「待退款」，不假装已退款。
    返回新的流水状态；没有流水或状态无需变更时返回原状态。
    """
    if not out_trade_no:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM payments WHERE out_trade_no=?",
                    (out_trade_no,))
        row = cur.fetchone()
        if row is None:
            return None
        status = row["status"]
        if status == "confirmed":
            new_status = "refund_pending"
        elif status == "submitted":
            new_status = "waived"
        else:
            return status
        cur.execute("UPDATE payments SET status=? WHERE out_trade_no=?",
                    (new_status, out_trade_no))
        conn.commit()
        return new_status
    finally:
        conn.close()


def apply_guarantee(folder, filename):
    """交付承诺：订单完成时间晚于承诺完成时间即超时免单。

    打印机端上报 completed 时调用。严格无缓冲容差：只要完成时刻 > eta_deadline
    就置 overtime=1、waived=1，并同步收款流水状态。已免单的不重复处理。
    返回处理结果字典；未超时/无承诺时间时返回 None。
    """
    job = get_job(folder, filename)
    if job is None:
        return None
    if job.get("waived"):
        return {"waived": True, "overtime": True,
                "reason": job.get("waive_reason")}
    deadline = job.get("eta_deadline")
    if not deadline:
        return None
    now = _now()
    # 字符串时间同格式（%Y-%m-%d %H:%M:%S）可直接比大小
    if now <= deadline:
        return None
    reason = f"打印完成时间 {now} 超过承诺完成时间 {deadline}，按交付承诺免单"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_jobs SET overtime=1, waived=1, waive_reason=?"
            " WHERE id=? AND (waived IS NULL OR waived=0)",
            (reason, job["id"]),
        )
        conn.commit()
        changed = cur.rowcount
    finally:
        conn.close()
    if not changed:
        return None
    payment_status = waive_payment(job.get("out_trade_no"))
    return {"waived": True, "overtime": True, "reason": reason,
            "out_trade_no": job.get("out_trade_no"),
            "payment_status": payment_status}


def request_reprint(order_id, reason):
    """用户申请免费重印：把订单置为「待审核」。

    只允许已完成、且未在申请中/已通过的订单重复申请。
    返回 True 表示申请已记录。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_jobs SET reprint_status='requested', reprint_reason=?"
            " WHERE id=? AND status='completed'"
            " AND (reprint_status IS NULL OR reprint_status='rejected')",
            ((reason or "").strip() or None, order_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_reprint_status(order_id, status):
    """更新重印申请处理状态（requested / approved / rejected）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE print_jobs SET reprint_status=? WHERE id=?",
                    (status, order_id))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_reprint_requests():
    """所有有重印申请记录的订单（用户端申请 + 管理端处理），最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE reprint_status IS NOT NULL"
            " ORDER BY id DESC")
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_job_progress(folder, filename, copies=None, pages=None):
    """记录打印进度（已提交份数 / 已打印页数）。

    只更新传入的字段；已完成/失败的订单不再改动，避免迟到的心跳把进度写回。
    返回受影响行数。
    """
    fields = {}
    if copies is not None:
        fields["progress_copies"] = max(_to_int(copies) or 0, 0)
    if pages is not None:
        fields["progress_pages"] = max(_to_int(pages) or 0, 0)
    if not fields:
        return 0
    fields["progress_at"] = _now()
    assignments = ", ".join(f"{key}=?" for key in fields)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE print_jobs SET {assignments}"
            " WHERE folder=? AND filename=? AND status!='completed'",
            list(fields.values()) + [folder, filename])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def mark_job_failure_alerted(job_id):
    """标记该失败订单已产生告警，避免告警引擎每轮重复推送。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE print_jobs SET failure_alerted=1 WHERE id=?", (job_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def load_open_jobs():
    """加载待打印订单，用于启动时恢复内存队列。

    只恢复 status='pending'：printing 说明上次已经交给打印机端（重启后重复入队会重打），
    failed 更不能自动重试。卡住的 printing 由 fail_stale_printing 超时兜底。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE status='pending' ORDER BY id ASC"
        )
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ----------------------- 定时打印（预约出纸） -----------------------

def list_due_scheduled_jobs(now=None):
    """到点该放行的预约订单：status='scheduled' 且 scheduled_at <= 当前时间。

    scheduled_at 与 created_at 同为 'YYYY-MM-DD HH:MM:SS'，
    同格式下字符串比较等价于时间比较，直接用 SQL 比即可。
    """
    current = now or _now()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE status='scheduled'"
            " AND scheduled_at IS NOT NULL AND scheduled_at<=?"
            " ORDER BY scheduled_at ASC",
            (current,))
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_job_status_by_id(job_id, status):
    """按 id 更新订单状态（预约放行 / 作废 / 取消用）。

    与 set_job_status 的区别：这里按 id 定位，适合「排队前」的预约单——
    此时订单还没有进入打印队列，用 folder+filename 反查容易和队列状态不同步。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE print_jobs SET status=?, status_at=? WHERE id=?",
                    (status, _now(), job_id))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def expire_stale_scheduled(hours=12):
    """预约单到点后长时间没人打印（设备离线 / 现场失联）时作废。

    只作用于「带预约时间」且仍在 scheduled 或 pending 的订单：
    即打订单（scheduled_at 为空）完全不受影响，避免误伤正常排队订单。
    作废代表不再打印、也不自动退款：按个人收款码的真实能力记为「待人工处理」。

    返回被作废的订单明细列表（含 id / folder / filename）。
    返回明细而非仅计数，是为了让调用方能据此把已放行、仍停留在内存打印
    队列里的 pending 预约单同步剔除，否则作废了 DB 状态、内存队列却照旧出纸。
    兼容旧调用方：len(返回值) 即原来的作废数量。
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT * FROM print_jobs"
            " WHERE status IN ('scheduled', 'pending')"
            " AND scheduled_at IS NOT NULL AND scheduled_at<?",
            (cutoff,)).fetchall()
        expired = [_row_to_job(row) for row in rows]
        if expired:
            ids = [job["id"] for job in expired]
            placeholders = ",".join("?" for _ in ids)
            cur.execute(
                "UPDATE print_jobs SET status='expired', status_at=?"
                f" WHERE id IN ({placeholders})",
                (_now(), *ids))
        conn.commit()
        return expired
    finally:
        conn.close()


def cancel_scheduled_job(job_id, user_id=None, folder=None):
    """用户取消一张还没到点的预约单，返回 (是否成功, 提示语, 订单)。

    只允许取消 status='scheduled'（尚未进打印队列）的订单；
    已经放行、正在打印或已完成的订单不能撤，如实告知。

    归属校验分两种情况，缺一不可：
    - 登录用户下单：订单记有 user_id，必须与当前登录用户一致；
    - 匿名会话下单：订单无 user_id，用下单时的会话目录（folder == 会话 token）校验。
    任一情况都不得放行无归属的取消请求，否则任意匿名会话能取消别人的预约单。
    """
    job = get_job_by_id(job_id)
    if job is None:
        return False, "订单不存在", None
    owner = job.get("user_id")
    if owner:
        # 已登录用户下单：必须本人才能取消
        if user_id is None or int(owner) != int(user_id):
            return False, "只能取消自己的订单", None
    else:
        # 匿名会话下单：没有 uid 可核，用会话 token 与订单目录比对
        if not folder or job.get("folder") != folder:
            return False, "只能取消自己的订单", None
    if job.get("status") != "scheduled":
        return False, "订单已开始打印或已完成，无法取消", job
    set_job_status_by_id(job_id, "canceled")
    job["status"] = "canceled"
    return True, "success", job


# ----------------------- 今日打印统计 -----------------------

def today_stats():
    """统计今天各小时的打印页数（只算已完成订单）。

    页数口径与计价一致：每单页数 =（打印范围结束 - 开始 + 1）× 份数。
    返回 24 个小时的桶，前端直接画柱状趋势图。
    """
    today = _now()[:10]
    hours = [{"hour": h, "pages": 0, "orders": 0} for h in range(24)]
    for job in list_jobs():
        created = job.get("created_at") or ""
        if not created.startswith(today) or job.get("status") != "completed":
            continue
        # 管理端测试页不计入经营趋势
        if job.get("folder") == "_selftest":
            continue
        try:
            hour = int(created[11:13])
        except (ValueError, IndexError):
            continue
        start = job.get("print_range_start") or 1
        end = job.get("print_range_end") or start
        copies = job.get("print_copies") or 1
        pages = max(int(end) - int(start) + 1, 0) * max(int(copies), 1)
        hours[hour]["pages"] += pages
        hours[hour]["orders"] += 1
    return {
        "date": today,
        "hours": hours,
        "total_pages": sum(h["pages"] for h in hours),
        "total_orders": sum(h["orders"] for h in hours),
    }


def report_jobs(start=None, end=None, tenant_id=None, device_id=None):
    """按日期/租户/设备筛选订单，供统计报表与 CSV 导出共用。

    start/end 为 "YYYY-MM-DD"；结束日包含当天（<= 23:59:59）。
    """
    sql = "SELECT * FROM print_jobs WHERE folder<>'_selftest'"
    args = []
    if start:
        sql += " AND created_at>=?"
        args.append(start + " 00:00:00")
    if end:
        sql += " AND created_at<=?"
        args.append(end + " 23:59:59")
    if tenant_id:
        sql += " AND tenant_id=?"
        args.append(tenant_id)
    if device_id:
        sql += " AND printer_id=?"
        args.append(device_id)
    sql += " ORDER BY id ASC"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _job_copies(job):
    """订单份数，异常值回退为 1。"""
    try:
        return max(int(job.get("print_copies") or 1), 1)
    except (TypeError, ValueError):
        return 1


def _job_pages(job):
    """订单页数（单份）：打印范围结束 - 开始 + 1。"""
    try:
        start = int(job.get("print_range_start") or 1)
        end = int(job.get("print_range_end") or start)
    except (TypeError, ValueError):
        return 0
    return max(end - start + 1, 0)


def _job_total_pages(job):
    """订单总页数：单份页数 × 份数（与 today_stats 口径一致）。"""
    return _job_pages(job) * _job_copies(job)


def _job_sheets(job):
    """订单实际耗纸张数：双面按 ceil(页/2)，单面按页数，再乘份数。"""
    pages = _job_pages(job)
    copies = _job_copies(job)
    side = str(job.get("print_side") or "one-sided")
    per_copy = (pages + 1) // 2 if side.startswith("two-sided") else pages
    return per_copy * copies


def report_summary(start=None, end=None, tenant_id=None, device_id=None):
    """统计报表：汇总 + 按天 + 按设备 + 按租户。

    口径（与计价/用纸保持一致）：
    - 页数 =（打印范围结束 - 开始 + 1）× 份数，只统计已完成订单；
    - 纸张 = 单面按页数、双面按向上取整(页数/2)，再乘份数；
    - 营收 = 已写入 out_trade_no 且未被免单（waived=0）的订单金额。
    """
    jobs = report_jobs(start, end, tenant_id, device_id)
    device_names = {d["device_id"]: (d.get("name") or d["device_id"])
                    for d in list_devices()}
    tenant_names = {t["tenant_id"]: (t.get("name") or t["tenant_id"])
                    for t in list_tenants()}

    daily, by_device, by_tenant = {}, {}, {}
    summary = {
        "orders_total": 0, "orders_completed": 0, "orders_failed": 0,
        "orders_paid": 0, "pages": 0, "sheets": 0, "revenue": 0.0,
        "saved": 0.0, "avg_price": 0.0,
    }
    for job in jobs:
        status = str(job.get("status") or "")
        day = str(job.get("created_at") or "")[:10]
        pages = _job_total_pages(job)
        sheets = _job_sheets(job)
        paid = bool(job.get("out_trade_no")) and not job.get("waived")
        amount = float(job.get("amount") or 0)
        saved = float(job.get("saved_amount") or 0)

        summary["orders_total"] += 1
        if status == "completed":
            summary["orders_completed"] += 1
            summary["pages"] += pages
            summary["sheets"] += sheets
            summary["saved"] += saved
        elif status == "failed":
            summary["orders_failed"] += 1
        if paid:
            summary["orders_paid"] += 1
            summary["revenue"] += amount

        bucket = daily.setdefault(day, {"date": day, "orders": 0, "completed": 0,
                                        "pages": 0, "revenue": 0.0})
        bucket["orders"] += 1
        if status == "completed":
            bucket["completed"] += 1
            bucket["pages"] += pages
        if paid:
            bucket["revenue"] += amount

        dev = job.get("printer_id") or "未指定"
        dbucket = by_device.setdefault(dev, {"device_id": dev, "orders": 0,
                                             "pages": 0, "revenue": 0.0})
        dbucket["orders"] += 1
        if status == "completed":
            dbucket["pages"] += pages
        if paid:
            dbucket["revenue"] += amount

        tid = job.get("tenant_id") or "default"
        tbucket = by_tenant.setdefault(tid, {"tenant_id": tid, "orders": 0,
                                             "pages": 0, "revenue": 0.0})
        tbucket["orders"] += 1
        if status == "completed":
            tbucket["pages"] += pages
        if paid:
            tbucket["revenue"] += amount

    total = summary["orders_total"]
    summary["completion_rate"] = round(
        summary["orders_completed"] / total * 100, 1) if total else 0.0
    summary["revenue"] = round(summary["revenue"], 2)
    summary["saved"] = round(summary["saved"], 2)
    summary["avg_price"] = round(
        summary["revenue"] / summary["orders_paid"], 2) if summary["orders_paid"] else 0.0

    for bucket in daily.values():
        bucket["revenue"] = round(bucket["revenue"], 2)
    for bucket in by_device.values():
        bucket["revenue"] = round(bucket["revenue"], 2)
        bucket["name"] = device_names.get(bucket["device_id"], bucket["device_id"])
    for bucket in by_tenant.values():
        bucket["revenue"] = round(bucket["revenue"], 2)
        bucket["name"] = tenant_names.get(bucket["tenant_id"], bucket["tenant_id"])

    return {
        "range": {"start": start, "end": end,
                  "tenant_id": tenant_id or "", "device_id": device_id or ""},
        "summary": summary,
        "daily": sorted(daily.values(), key=lambda x: x["date"]),
        "by_device": sorted(by_device.values(),
                            key=lambda x: x["revenue"], reverse=True),
        "by_tenant": sorted(by_tenant.values(),
                            key=lambda x: x["revenue"], reverse=True),
    }


# ----------------------- 设备指令 device_commands -----------------------
# 管理端下发指令（目前只有「重启打印机端」），客户端每秒轮询取走并执行。
# 超过 10 分钟没被取走的指令自动过期，避免设备离线很久后突然重启。

_COMMAND_TTL_MINUTES = 10


def add_command(device_id, command):
    """新增一条待执行指令，返回指令 id。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO device_commands (device_id, command, status, created_at, updated_at)"
            " VALUES (?, ?, 'pending', ?, ?)",
            (device_id, command, _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def take_pending_commands(device_id, limit=5):
    """客户端取走待执行指令：先把超时的标记 expired，再把 pending 改为 sent。

    返回 [{id, command, created_at}]，客户端执行后调用 finish_command 回报结果。
    """
    cutoff = (datetime.now() - timedelta(minutes=_COMMAND_TTL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE device_commands SET status='expired', updated_at=?"
            " WHERE device_id=? AND status='pending' AND created_at<?",
            (_now(), device_id, cutoff),
        )
        cur.execute(
            "SELECT id, command, created_at FROM device_commands"
            " WHERE device_id=? AND status='pending' ORDER BY id ASC LIMIT ?",
            (device_id, limit),
        )
        rows = cur.fetchall()
        for row in rows:
            cur.execute(
                "UPDATE device_commands SET status='sent', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        conn.commit()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def finish_command(command_id, ok, result=""):
    """客户端回报指令执行结果：ok=True → done，否则 failed。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE device_commands SET status=?, result=?, updated_at=? WHERE id=?",
            ("done" if ok else "failed", (result or "")[:500], _now(), command_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_command(command_id):
    """按 id 查询一条设备指令，不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM device_commands WHERE id=?", (command_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_commands(device_id, limit=10):
    """管理端查看某台设备最近的指令及执行结果。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM device_commands WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (device_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ----------------------- 运维 agent 处理记录 agent_runs -----------------------


def add_agent_run(kind, device_id, verdict, reason, action, action_status,
                  alert_id=None, job_folder=None, job_filename=None,
                  command_id=None):
    """记一条 agent 处置留痕，返回 id（管理端「证据 → 诊断 → 动作」链路可查）。"""
    now = _now()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_runs (kind, device_id, alert_id, job_folder,"
            " job_filename, verdict, reason, action, action_status, command_id,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, device_id, alert_id, job_folder, job_filename, verdict,
             reason, action, action_status, command_id, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def set_agent_run_command(run_id, command_id, action_status):
    """回填 agent 下发指令的 id 与状态（后续由指令结果人工核对）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agent_runs SET command_id=?, action_status=?, updated_at=? WHERE id=?",
            (command_id, action_status, _now(), run_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_agent_runs(device_id=None, limit=50):
    """管理端查看 agent 处置记录，最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        if device_id:
            cur.execute(
                "SELECT * FROM agent_runs WHERE device_id=? ORDER BY id DESC LIMIT ?",
                (device_id, limit))
        else:
            cur.execute(
                "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def last_agent_action_at(device_id, action):
    """该设备上一次同类动作的时间（冷却窗口用）；没有记录返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT created_at FROM agent_runs WHERE device_id=? AND action=?"
            " ORDER BY id DESC LIMIT 1", (device_id, action))
        row = cur.fetchone()
        return row["created_at"] if row else None
    finally:
        conn.close()


def has_agent_run(device_id, action, job_folder=None, job_filename=None):
    """该设备（可指定订单）是否已经有过同类动作，避免重复处置同一单。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        if job_folder is not None and job_filename is not None:
            cur.execute(
                "SELECT 1 FROM agent_runs WHERE device_id=? AND action=?"
                " AND job_folder=? AND job_filename=? LIMIT 1",
                (device_id, action, job_folder, job_filename))
        else:
            cur.execute(
                "SELECT 1 FROM agent_runs WHERE device_id=? AND action=? LIMIT 1",
                (device_id, action))
        return cur.fetchone() is not None
    finally:
        conn.close()


def has_agent_run_for_alert(alert_id):
    """该条告警是否已被 agent 上报过，避免同一告警重复落库。"""
    if alert_id is None:
        return False
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM agent_runs WHERE alert_id=? LIMIT 1", (alert_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def list_stale_printing_jobs(seconds):
    """卡在 printing 超过 seconds 的订单（含所属设备）。"""
    cutoff = (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs WHERE status='printing'"
            " AND status_at IS NOT NULL AND status_at<? ORDER BY id ASC", (cutoff,))
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ----------------------- 设备日志 device_logs -----------------------

_LOG_KEEP_LINES = 300


def set_device_logs(device_id, lines):
    """保存客户端推上来的日志（只保留最近 300 行）。"""
    lines = [str(line)[:500] for line in (lines or [])][-_LOG_KEEP_LINES:]
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO device_logs (device_id, content, updated_at)"
            " VALUES (?, ?, ?)",
            (device_id, json.dumps(lines, ensure_ascii=False), _now()),
        )
        conn.commit()
        return len(lines)
    finally:
        conn.close()


def get_device_logs(device_id, limit=200):
    """读取某台设备最近推上来的日志，返回 (lines, updated_at)。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM device_logs WHERE device_id=?", (device_id,))
        row = cur.fetchone()
        if row is None:
            return [], None
        try:
            lines = json.loads(row["content"] or "[]")
        except (TypeError, ValueError):
            lines = []
        # limit 来自查询参数：0 会变成 lines[0:]（返回全部），负数会变成 lines[1:]（丢首行）
        limit = max(1, min(int(limit), 500))
        return lines[-limit:], row["updated_at"]
    finally:
        conn.close()


# ----------------------- 设备 devices -----------------------

def _load_printers(raw):
    """设备上报的本机打印机队列列表（JSON）；坏数据返回空列表。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _row_to_device(row):
    """把数据库行转成设备字典，并把传感器 0/1 还原为布尔值。"""
    device = dict(row)

    def _bool(value):
        return None if value is None else bool(value)

    device["ups_online"] = _bool(device.get("ups_online"))
    device["door_open"] = _bool(device.get("door_open"))
    device["supports_color"] = _bool(device.get("supports_color"))
    device["supports_duplex"] = _bool(device.get("supports_duplex"))
    device["printers"] = _load_printers(device.get("printers"))
    return device


def register_device(device_id, name="", model="", driver="", conn="", paper_capacity=0,
                    paper_state=None, printers=None, supports_color=None,
                    supports_duplex=None, tenant_id=None):
    """注册（或更新）一台设备，返回最新的设备字典。

    tenant_id 为设备归属的租户；未提供时保持原值（新设备默认归 default 租户）。
    """
    conn_ = _connect()
    try:
        cur = conn_.cursor()
        cur.execute(
            "SELECT id, pages_printed, paper_baseline_pages FROM devices WHERE device_id=?",
            (device_id,),
        )
        row = cur.fetchone()
        exists = row is not None
        if exists:
            # 名称为空时不覆盖已有名称：设备端每次启动都会重新注册，
            # 不能让它把管理端改好的展示名冲回设备 ID。
            fields = "model=?, driver=?, conn=?, paper_capacity=?"
            params = [model, driver, conn, _to_int(paper_capacity)]
            if name:
                fields = "name=?, " + fields
                params.insert(0, name)
            # 容量变了就按同一套估算重算剩余张数，避免出现「剩余 112 > 容量 100」
            capacity = _to_int(paper_capacity)
            if capacity:
                used = ((_to_int(row["pages_printed"]) or 0)
                        - (_to_int(row["paper_baseline_pages"]) or 0))
                fields += ", paper_left=?"
                params.append(max(capacity - max(used, 0), 0))
            # paper_state 未被提供时不覆盖原有值
            if paper_state is not None:
                fields += ", paper_state=?"
                params.append(paper_state)
            if printers is not None:
                fields += ", printers=?"
                params.append(json.dumps(printers, ensure_ascii=False))
            # 能力只在尚未设置时写入，避免设备每次启动把管理端手动改的值冲掉
            if supports_color is not None:
                fields += ", supports_color=COALESCE(supports_color, ?)"
                params.append(1 if supports_color else 0)
            if supports_duplex is not None:
                fields += ", supports_duplex=COALESCE(supports_duplex, ?)"
                params.append(1 if supports_duplex else 0)
            # 设备端上报 TENANT_ID 时更新归属；未上报时不覆盖原值
            if tenant_id:
                fields += ", tenant_id=?"
                params.append(tenant_id)
            params.append(device_id)
            cur.execute(
                f"UPDATE devices SET {fields} WHERE device_id=?", params
            )
        else:
            cur.execute(
                """
                INSERT INTO devices
                    (device_id, name, model, driver, conn, status,
                     paper_capacity, pages_printed, paper_left, last_heartbeat, created_at,
                     paper_state, paper_baseline_pages, printers,
                     supports_color, supports_duplex, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    name,
                    model,
                    driver,
                    conn,
                    "offline",
                    _to_int(paper_capacity),
                    0,
                    # 新注册设备默认满纸：paper_left 初值 = 纸盒容量，
                    # 避免从未上报过纸量的设备在列表里显示为 None
                    _to_int(paper_capacity) or None,
                    None,
                    _now(),
                    paper_state,
                    # 纸量估算基线：注册时起算为 0，配合 paper_left=容量 表示“刚装满”
                    0,
                    json.dumps(printers or [], ensure_ascii=False),
                    None if supports_color is None else (1 if supports_color else 0),
                    None if supports_duplex is None else (1 if supports_duplex else 0),
                    tenant_id or "default",
                ),
            )
        conn_.commit()
    finally:
        conn_.close()
    return get_device(device_id)


def update_device_config(device_id, **fields):
    """更新设备的展示名、门店地址、经纬度与能力参数。

    只更新传入的字段；返回最新设备字典，设备不存在返回 None。
    """
    allowed = {"name", "store_name", "address", "business_hours", "lat", "lng",
               "supports_color", "supports_duplex", "speed_ppm"}
    updates = {key: value for key, value in fields.items()
               if key in allowed and value is not None}
    if not updates:
        return get_device(device_id)
    for key in ("supports_color", "supports_duplex"):
        if key in updates:
            updates[key] = 1 if updates[key] else 0
    if "speed_ppm" in updates:
        updates["speed_ppm"] = _to_float(updates["speed_ppm"])
    if "lat" in updates:
        updates["lat"] = _to_float(updates["lat"])
    if "lng" in updates:
        updates["lng"] = _to_float(updates["lng"])
    assignments = ", ".join(f"{key}=?" for key in updates)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE devices SET {assignments} WHERE device_id=?",
            list(updates.values()) + [device_id],
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_device(device_id)


def _paper_baseline(used_sheets, capacity, left):
    """按「已用张数 = 容量 − 剩余张数」重定位纸量估算基线。

    paper_left = 容量 − (累计张数 − 基线)，因此令
        基线 = 累计张数 − (容量 − 剩余)
    即可让估算严格从本次上报/设定的剩余量继续递减：
    容量 250、剩余 122 → 基线 = 累计 − 128，再打 5 张 → 117（而不是跳回 245）。
    剩余 = 容量（补满）时基线 = 累计，等价于计数清零。
    容量未知则无法换算，退化为基线 = 累计（视为刚补满）。
    """
    if capacity:
        return used_sheets - max(capacity - left, 0)
    return used_sheets


def set_paper_config(device_id, paper_capacity=None, paper_left=None):
    """管理端设置设备的纸盒容量 / 当前剩余张数，返回最新设备字典。

    设备不存在时返回 None。语义：
    - 只给容量：视为「刚补满」，paper_left = 容量，计数归零；
    - 给了剩余：按 _paper_baseline 重定位基线（设置剩余量即一次校准）；
    - 两个都为 None：不改变任何字段，返回现状。
    注意这里只维护纸量字段，不会刷新 last_heartbeat / status，
    以免把离线设备误标成在线、掩盖 R2 离线告警。
    """
    current = get_device(device_id)
    if current is None:
        return None
    capacity = _to_int(paper_capacity)
    left = _to_int(paper_left)
    if capacity is None and left is None:
        return current
    new_capacity = capacity if capacity is not None else (_to_int(current.get("paper_capacity")) or 0)
    if left is None:
        left = new_capacity or None
    used_sheets = _to_int(current.get("pages_printed")) or 0
    baseline = _paper_baseline(used_sheets, new_capacity, left if left is not None else 0)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE devices SET paper_capacity=?, paper_left=?, paper_baseline_pages=?"
            " WHERE device_id=?",
            (new_capacity, left, baseline, device_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_device(device_id)


def update_heartbeat(device_id, status="online", pages_printed=None, paper_left=None,
                     ups_online=None, door_open=None, pickup_seq=None, paper_state=None,
                     ups_runtime_min=None, printers=None, tenant_id=None):
    """更新设备心跳与相关字段，返回最新设备字典；设备不存在时返回 None。

    只有心跳里上报了的字段才会被更新（未上报的传感器字段保持原值/为空）。

    纸量估算采用「基线」方式：显式上报 paper_left 时以其为准，并把基线重定位到
    「已用张数 = 容量 − 上报剩余量」；此后只上报页数时按
    paper_left = 容量 - (累计张数 - 基线) 估算，即从上次上报的剩余量继续递减。
    这样补纸（上报剩余 = 容量）后计数自动归零，人工核对剩余量后也不会跳变。
    注意：累计计数口径是「张」（见 add_sheets_used），不是「页」。
    """
    current = get_device(device_id)
    if current is None:
        return None
    capacity = _to_int(current.get("paper_capacity"))
    baseline = _to_int(current.get("paper_baseline_pages")) or 0
    pages = _to_int(pages_printed)
    left = _to_int(paper_left)

    fields = ["last_heartbeat=?", "status=?"]
    params = [_now(), status]
    # 仅更新有上报的字段
    for column, value in (
        ("ups_online", _to_bool_int(ups_online)),
        ("door_open", _to_bool_int(door_open)),
        ("pickup_seq", _to_int(pickup_seq)),
        ("ups_runtime_min", _to_int(ups_runtime_min)),
    ):
        if value is not None:
            fields.append(f"{column}=?")
            params.append(value)
    if pages is not None:
        fields.append("pages_printed=?")
        params.append(pages)
    # 纸量：显式上报优先，并把它当作一次「校准」（补纸/换纸/人工核对）
    if left is not None:
        fields.append("paper_left=?")
        params.append(left)
        # 基线重定位：已用张数 = 容量 − 上报剩余量，使估算从上报值继续递减
        # （详见 _paper_baseline 的说明）
        current_used = pages if pages is not None else (_to_int(current.get("pages_printed")) or 0)
        fields.append("paper_baseline_pages=?")
        params.append(_paper_baseline(current_used, capacity, left))
    elif pages is not None and capacity:
        used = max(pages - baseline, 0)
        fields.append("paper_left=?")
        params.append(max(capacity - used, 0))
    # 纸张状态档位为字符串（full/half/low/empty），单独处理（不做数值转换）
    if paper_state is not None:
        fields.append("paper_state=?")
        params.append(paper_state)
    if printers is not None:
        fields.append("printers=?")
        params.append(json.dumps(printers, ensure_ascii=False))
    # 设备端上报 TENANT_ID 时修正归属；未上报时不覆盖原值
    if tenant_id:
        fields.append("tenant_id=?")
        params.append(tenant_id)
    params.append(device_id)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE devices SET {', '.join(fields)} WHERE device_id=?", params
        )
        conn.commit()
    finally:
        conn.close()
    return get_device(device_id)


def list_devices(tenant_id=None):
    """返回设备；传 tenant_id 时只返回该租户的设备。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        if tenant_id:
            cur.execute("SELECT * FROM devices WHERE tenant_id=? ORDER BY id ASC",
                        (tenant_id,))
        else:
            cur.execute("SELECT * FROM devices ORDER BY id ASC")
        return [_row_to_device(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_device(device_id):
    """按 device_id 查询单台设备，不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM devices WHERE device_id=?", (device_id,))
        row = cur.fetchone()
        return _row_to_device(row) if row else None
    finally:
        conn.close()


def delete_device(device_id):
    """从数据库删除一台设备，并清理它名下的告警、指令、日志。

    该设备未完成的订单会被标记为 failed：设备都删了，这些订单永远不会有人来打，
    留在队列里只会让用户端一直显示「待打印」。
    返回 (是否删除成功, 清理的告警条数, 标记失败的订单数)。
    """
    if get_device(device_id) is None:
        return False, 0, 0
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_jobs SET status='failed', status_at=?"
            " WHERE printer_id=? AND status!='completed'",
            (_now(), device_id),
        )
        failed_jobs = cur.rowcount
        # 先清理该设备的告警，避免设备删掉后大屏上残留孤儿告警
        cur.execute("DELETE FROM alerts WHERE device_id=?", (device_id,))
        removed_alerts = cur.rowcount
        cur.execute("DELETE FROM device_commands WHERE device_id=?", (device_id,))
        cur.execute("DELETE FROM device_logs WHERE device_id=?", (device_id,))
        # 设备删除后令牌一并吊销，避免旧终端继续用旧令牌上报
        cur.execute("DELETE FROM device_tokens WHERE device_id=?", (device_id,))
        cur.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted, removed_alerts, failed_jobs
    finally:
        conn.close()


def get_device_by_printer_id(printer_id):
    """按 printer_id 查询设备。

    约定：print_jobs.printer_id 存的是设备表主键 device_id 的值，
    因此直接复用 get_device(device_id)。
    """
    return get_device(printer_id)


def _hash_secret(value):
    """配对密钥与设备令牌统一做 sha256，库里不落明文。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def create_device_claim(claim_id, code, secret, hostname="", fingerprint="",
                        model="", printers=None, paper_capacity=0,
                        selected_printer="", ttl_seconds=1800):
    """登记一次待批的配对申请，返回申请字典（不含 secret 明文）。"""
    now = int(datetime.now().timestamp())
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO device_claims
                (claim_id, code, secret_hash, hostname, fingerprint, model,
                 selected_printer, printers, paper_capacity, status,
                 created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (claim_id, code, _hash_secret(secret), hostname, fingerprint,
             model, selected_printer,
             json.dumps(printers or [], ensure_ascii=False),
             _to_int(paper_capacity) or 0, now, now + int(ttl_seconds)),
        )
        conn.commit()
        return get_device_claim(claim_id)
    finally:
        conn.close()


def _row_to_claim(row):
    claim = dict(row)
    claim["printers"] = json.loads(claim.get("printers") or "[]")
    return claim


def get_device_claim(claim_id):
    """按 claim_id 取配对申请，不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM device_claims WHERE claim_id=?", (claim_id,))
        row = cur.fetchone()
        return _row_to_claim(row) if row else None
    finally:
        conn.close()


def expire_device_claims():
    """把超过有效期的 pending 申请标记为 expired，返回处理条数。"""
    now = int(datetime.now().timestamp())
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE device_claims SET status='expired'"
            " WHERE status='pending' AND expires_at<?", (now,))
        expired = cur.rowcount
        # 已批准但客户端一直没来取令牌的申请：1 小时后清掉明文，
        # 避免长期把可用令牌明文留在库里
        cur.execute(
            "UPDATE device_claims SET token_plain=NULL"
            " WHERE token_plain IS NOT NULL AND status='bound' AND bound_at<?",
            (now - 3600,))
        conn.commit()
        return expired
    finally:
        conn.close()


def list_pending_device_claims():
    """管理端待接入列表：过期的先清掉，其余按申请时间倒序。"""
    expire_device_claims()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM device_claims WHERE status='pending'"
            " ORDER BY created_at DESC")
        return [_row_to_claim(row) for row in cur.fetchall()]
    finally:
        conn.close()


def approve_device_claim(claim_id, device_id, token_plain):
    """批准配对：写入设备令牌（只存哈希），把令牌明文暂存给客户端取一次。"""
    now = int(datetime.now().timestamp())
    token_hash = _hash_secret(token_plain)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE device_claims SET status='bound', device_id=?, token_plain=?,"
            " bound_at=? WHERE claim_id=? AND status='pending'",
            (device_id, token_plain, now, claim_id))
        if cur.rowcount == 0:
            conn.rollback()
            return None
        cur.execute(
            "INSERT INTO device_tokens (device_id, token_hash, created_at, revoked)"
            " VALUES (?, ?, ?, 0)"
            " ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash,"
            " created_at=excluded.created_at, revoked=0",
            (device_id, token_hash, now))
        conn.commit()
        return get_device_claim(claim_id)
    finally:
        conn.close()


def take_claim_token(claim_id):
    """客户端取走一次性令牌明文，取走后立即清空，避免被重复读取。

    先读出明文，再用带 `token_plain IS NOT NULL` 的条件更新占位：
    并发的第二次请求 rowcount 为 0，拿不到令牌，保证「只发一次」。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT token_plain FROM device_claims WHERE claim_id=?",
                    (claim_id,))
        row = cur.fetchone()
        if row is None or not row["token_plain"]:
            return None
        token = row["token_plain"]
        cur.execute(
            "UPDATE device_claims SET token_plain=NULL"
            " WHERE claim_id=? AND token_plain IS NOT NULL", (claim_id,))
        conn.commit()
        return token if cur.rowcount == 1 else None
    finally:
        conn.close()


def reject_device_claim(claim_id):
    """管理端拒绝一次配对申请。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE device_claims SET status='rejected'"
            " WHERE claim_id=? AND status='pending'", (claim_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def device_id_by_token(token):
    """用设备令牌反查 device_id；无匹配或已吊销返回 None。"""
    if not token:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT device_id FROM device_tokens WHERE token_hash=? AND revoked=0",
            (_hash_secret(token),))
        row = cur.fetchone()
        return row["device_id"] if row else None
    finally:
        conn.close()


def _row_to_eject_event(row):
    return {
        "event_id": row["event_id"],
        "event": row["event"],
        "source": row["source"],
        "ts": row["ts"],
        "confidence": row["confidence"],
        "created_at": row["created_at"],
        "order_id": row["order_id"] if "order_id" in row.keys() else None,
        "has_photo": bool(row["has_photo"]) if "has_photo" in row.keys() else False,
    }


def save_eject_photo(device_id, event_id, image, mime="image/jpeg"):
    """保存出纸现场图；同一事件重复上报时保留第一次的图。"""
    if not image:
        raise ValueError("现场图为空")
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO eject_photos"
            " (device_id, event_id, image, mime, created_at) VALUES (?, ?, ?, ?, ?)",
            (device_id, event_id, sqlite3.Binary(image), mime, _now()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_eject_photo(device_id, event_id):
    """取出某条出纸事件的现场图，返回 (bytes, mime)；没有图时返回 (None, None)。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT image, mime FROM eject_photos WHERE device_id=? AND event_id=?",
            (device_id, event_id),
        ).fetchone()
        if row is None:
            return None, None
        return bytes(row["image"]), row["mime"]
    finally:
        conn.close()


def add_eject_event(device_id, event, order_id=None):
    """写入出纸事件；相同设备和 event_id 重试时返回原记录。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        created_at = _now()
        cur.execute(
            "INSERT OR IGNORE INTO eject_events "
            "(device_id, event_id, event, source, ts, confidence, created_at, order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, event["event_id"], event["event"], event["source"],
             event["ts"], event.get("confidence"), created_at, order_id),
        )
        conn.commit()
        cur.execute(
            "SELECT * FROM eject_events WHERE device_id=? AND event_id=?",
            (device_id, event["event_id"]),
        )
        row = cur.fetchone()
        same = all(row[key] == event.get(key) for key in
                   ("event", "source", "ts", "confidence"))
        return _row_to_eject_event(row) if same else None
    finally:
        conn.close()


def list_eject_events_for_order(order_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT e.*, (p.event_id IS NOT NULL) AS has_photo FROM eject_events e"
            " LEFT JOIN eject_photos p"
            " ON p.device_id=e.device_id AND p.event_id=e.event_id"
            " WHERE e.order_id=? ORDER BY e.ts DESC, e.id DESC",
             (int(order_id),),
        )
        return [_row_to_eject_event(row) for row in cur.fetchall()]
    finally:
        conn.close()


def list_eject_events(device_id, limit=100, since=None):
    limit = max(1, min(int(limit), 500))
    sql = ("SELECT e.*, (p.event_id IS NOT NULL) AS has_photo FROM eject_events e"
           " LEFT JOIN eject_photos p"
           " ON p.device_id=e.device_id AND p.event_id=e.event_id"
           " WHERE e.device_id=?")
    args = [device_id]
    if since is not None:
        sql += " AND e.ts>=?"
        args.append(int(since))
    sql += " ORDER BY e.ts DESC, e.id DESC LIMIT ?"
    args.append(limit)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        return [_row_to_eject_event(row) for row in cur.fetchall()]
    finally:
        conn.close()


def printing_jobs_without_eject(device_id, stuck_seconds=300):
    """本设备卡在 printing 且始终没有出纸事件的订单，供 R6 出纸异常告警。

    判定口径：status 仍为 printing、进入 printing 已超过 stuck_seconds，
    且该订单没有任何 eject_events 记录（X5 上报出纸时会把 order_id 绑定到当前活动订单）。
    """
    cutoff = (datetime.now() - timedelta(seconds=int(stuck_seconds))).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_jobs j WHERE j.printer_id=? AND j.status='printing'"
            " AND j.status_at IS NOT NULL AND j.status_at<=?"
            " AND NOT EXISTS (SELECT 1 FROM eject_events e WHERE e.order_id=j.id)"
            " ORDER BY j.id ASC",
            (device_id, cutoff))
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


def add_sheets_used(device_id, sheets):
    """把本次打印「实际耗纸张数」累加到设备的计数，并同步重算 paper_left。

    口径说明：入参与累计值都是「张」，不是「页」。
    单面打印 n 页 = n 张；双面打印 n 页 = ceil(n/2) 张；再乘以份数。
    （数据库列名沿用历史上的 pages_printed，不改列名以免动到老库结构，
      但它存的是「累计已用纸张数」。）

    paper_left = max(paper_capacity - (累计张数 - paper_baseline_pages), 0)；
    基线只在设备上报 paper_left（补纸/校准）时复位，因此补纸后不会长期假报缺纸。
    当 paper_capacity 为 0 或空时无法估算，paper_left 置 None。
    设备不存在时返回 None。
    """
    sheets = _to_int(sheets) or 0
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pages_printed, paper_capacity, paper_baseline_pages "
            "FROM devices WHERE device_id=?",
            (device_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        used_sheets = (_to_int(row["pages_printed"]) or 0) + sheets
        capacity = _to_int(row["paper_capacity"])
        baseline = _to_int(row["paper_baseline_pages"]) or 0
        if capacity:
            used = max(used_sheets - baseline, 0)
            paper_left = max(capacity - used, 0)
        else:
            paper_left = None
        cur.execute(
            "UPDATE devices SET pages_printed=?, paper_left=? WHERE device_id=?",
            (used_sheets, paper_left, device_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_device(device_id)


# ----------------------- 价格配置 price_config -----------------------
# 单行配置（id=1）。单面按「页」计价，双面按「张」计价：
#   单面 黑白 0.3 元/页、彩色 0.8 元/页；
#   双面 黑白 0.5 元/张（一张纸印两面）、彩色 1.5 元/张。
# 双面奇数页时，最后剩下的一页按单面价计。
# 后端 /pay/bill/create 结算、管理端价格页、用户端显示都读这里，保证同价。
DEFAULT_PRICE = {"bw": 0.3, "color": 0.8, "bw_duplex": 0.5, "color_duplex": 1.5}

# 配置键 → 数据库列名，读写两处共用，避免字段对不上
_PRICE_FIELDS = (
    ("bw", "bw_price"),
    ("color", "color_price"),
    ("bw_duplex", "bw_duplex_price"),
    ("color_duplex", "color_duplex_price"),
)


def _price_from_row(row):
    """从 price_config / device_price 的一行里取值，NULL 回落到默认价。"""
    result = {}
    for key, column in _PRICE_FIELDS:
        value = _to_float(row[column])
        result[key] = value
    return result


def get_price_config():
    """读取当前单价配置；未配置过时返回默认值（updated_at 为 None）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM price_config WHERE id=1")
        row = cur.fetchone()
        if row is None:
            return dict(DEFAULT_PRICE, updated_at=None)
        values = _price_from_row(row)
        return dict(
            {key: (values[key] if values[key] is not None else DEFAULT_PRICE[key])
             for key, _ in _PRICE_FIELDS},
            updated_at=row["updated_at"],
        )
    finally:
        conn.close()


def set_price_config(bw=None, color=None, bw_duplex=None, color_duplex=None):
    """管理端修改单价，返回最新配置。

    只传部分字段时，其余字段保持原值；全部为空时返回现状不改动。
    """
    incoming = {"bw": bw, "color": color,
                "bw_duplex": bw_duplex, "color_duplex": color_duplex}
    parsed = {key: _to_float(value) for key, value in incoming.items()}
    if all(value is None for value in parsed.values()):
        return get_price_config()
    current = get_price_config()
    merged = {key: (parsed[key] if parsed[key] is not None else current[key])
              for key, _ in _PRICE_FIELDS}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO price_config"
            " (id, bw_price, color_price, bw_duplex_price, color_duplex_price, updated_at)"
            " VALUES (1, ?, ?, ?, ?, ?)",
            (merged["bw"], merged["color"], merged["bw_duplex"],
             merged["color_duplex"], _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_price_config()


# ----------------------- 单台设备独立定价 device_price -----------------------
# 每台机器可以有自己的单价；没有记录的设备跟随全局 price_config。
# 用户端选机后按该机价格预估，后端 /pay/bill/create 也按同一份价结算。

def _row_to_device_price(row):
    values = _price_from_row(row)
    return dict(values, device_id=row["device_id"], updated_at=row["updated_at"])


def get_device_price(device_id):
    """读取某台设备的独立定价，没设置过返回 None。"""
    if not device_id:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM device_price WHERE device_id=?", (device_id,))
        row = cur.fetchone()
        return _row_to_device_price(row) if row else None
    finally:
        conn.close()


def set_device_price(device_id, bw=None, color=None,
                     bw_duplex=None, color_duplex=None):
    """设置某台设备的独立单价；只传部分字段时其余保持原值。

    返回该设备最新的独立定价字典。
    """
    incoming = {"bw": bw, "color": color,
                "bw_duplex": bw_duplex, "color_duplex": color_duplex}
    parsed = {key: _to_float(value) for key, value in incoming.items()}
    current = get_device_price(device_id) or {key: None for key, _ in _PRICE_FIELDS}
    merged = {key: (parsed[key] if parsed[key] is not None else current.get(key))
              for key, _ in _PRICE_FIELDS}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO device_price"
            " (device_id, bw_price, color_price, bw_duplex_price,"
            "  color_duplex_price, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, merged["bw"], merged["color"], merged["bw_duplex"],
             merged["color_duplex"], _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_device_price(device_id)


def clear_device_price(device_id):
    """清除某台设备的独立定价，使其恢复跟随全局单价。返回是否有记录被删。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM device_price WHERE device_id=?", (device_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ----------------------- 租户 / 订阅 / 租户默认价 -----------------------
# 软件订阅线的多租户最小骨架：一个租户 = 一个客户（打印点 / 学校文印室）。
# 平台方用全局 SECRET_KEY 开通与维护租户；租户用自己的密钥只能看/改自己租户的数据。
# 本阶段订阅为「手动开通、手动续期」，不接支付网关、不做自动计费。

def _row_to_tenant(row):
    """tenants 行 → 字典（含订阅信息）。"""
    if row is None:
        return None
    return dict(row)


def _tenant_with_subscription(tenant):
    """给租户字典附上订阅信息，便于接口一次返回。"""
    if not tenant:
        return tenant
    tenant = dict(tenant)
    tenant["subscription"] = get_subscription(tenant["tenant_id"])
    return tenant


DEFAULT_FEE_RATE = 0.1


def _fee_rate(value):
    """平台抽成比例：0–1，非法值退回默认 10%。"""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return DEFAULT_FEE_RATE
    return min(max(rate, 0.0), 1.0)


def create_tenant(tenant_id, name="", secret_key=None, plan="starter",
                  printer_limit=1, expires_at=None, contact="", note="",
                  fee_rate=0.1):
    """开通一个租户（幂等保护：tenant_id 已存在则返回 None）。

    未指定 secret_key 时自动生成一个随机密钥（返回体里能看到，便于交付给客户）。
    同时写入一条初始订阅记录。返回租户字典（含 subscription）。
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return None
    if get_tenant(tenant_id):
        return None
    key = (secret_key or "").strip() or ("sk_" + secrets.token_urlsafe(24))
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tenants"
            " (tenant_id, name, secret_key, status, contact, note, created_at)"
            " VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (tenant_id, name or tenant_id, key, contact, note, _now()),
        )
        cur.execute(
            "INSERT OR REPLACE INTO subscriptions"
            " (tenant_id, plan, printer_limit, status, started_at, expires_at,"
            " note, updated_at, fee_rate)"
            " VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (tenant_id, plan, _to_int(printer_limit), _now(), expires_at,
             "手动开通", _now(), _fee_rate(fee_rate)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_tenant(tenant_id)


def get_tenant(tenant_id):
    """按 tenant_id 读取租户（含订阅），不存在返回 None。"""
    if not tenant_id:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    return _tenant_with_subscription(_row_to_tenant(row))


def get_tenant_by_secret(secret_key):
    """按密钥反查租户（含订阅）；没有匹配返回 None。

    遍历所有租户用 hmac.compare_digest 常量时间比较，避免按响应时间逐字节
    推断出租户密钥。仅返回 active 状态的租户密钥匹配结果。
    """
    import hmac
    if not secret_key:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tenants")
        rows = cur.fetchall()
    finally:
        conn.close()
    match = None
    for row in rows:
        stored = row["secret_key"] or ""
        if stored and hmac.compare_digest(stored, str(secret_key)):
            match = dict(row)
    if match is None or match.get("status") != "active":
        return None
    return _tenant_with_subscription(match)


def list_tenants():
    """列出全部租户（含订阅），供平台方管理端展示。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tenants ORDER BY id ASC")
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_tenant_with_subscription(dict(row)) for row in rows]


def set_tenant_status(tenant_id, status):
    """启用 / 停用一个租户；停用后该租户密钥失效。返回最新租户或 None。"""
    if status not in ("active", "suspended"):
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET status=? WHERE tenant_id=?", (status, tenant_id))
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_tenant(tenant_id)


def get_subscription(tenant_id):
    """读取租户当前订阅，没有返回 None。"""
    if not tenant_id:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscriptions WHERE tenant_id=?", (tenant_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_subscription(tenant_id, plan=None, printer_limit=None,
                     expires_at=None, status=None, note=None,
                     fee_rate=None):
    """更新租户订阅（续期 / 改套餐 / 改状态 / 改抽成）；租户必须存在。

    只更新传入的字段，返回最新订阅字典；租户不存在返回 None。
    """
    if get_tenant(tenant_id) is None:
        return None
    current = get_subscription(tenant_id) or {}
    merged = {
        "plan": plan if plan is not None else current.get("plan"),
        "printer_limit": (_to_int(printer_limit) if printer_limit is not None
                          else current.get("printer_limit")),
        "expires_at": expires_at if expires_at is not None else current.get("expires_at"),
        "status": status if status is not None else (current.get("status") or "active"),
        "note": note if note is not None else current.get("note"),
        "fee_rate": (_fee_rate(fee_rate) if fee_rate is not None
                     else _fee_rate(current.get("fee_rate"))),
    }
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO subscriptions"
            " (tenant_id, plan, printer_limit, status, started_at, expires_at,"
            " note, updated_at, fee_rate)"
            " VALUES (?, ?, ?, ?, COALESCE((SELECT started_at FROM subscriptions"
            " WHERE tenant_id=?), ?), ?, ?, ?, ?)",
            (tenant_id, merged["plan"], merged["printer_limit"], merged["status"],
             tenant_id, _now(), merged["expires_at"], merged["note"], _now(),
             merged["fee_rate"]),
        )
        conn.commit()
    finally:
        conn.close()
    return get_subscription(tenant_id)


def _row_to_tenant_price(row):
    if row is None:
        return None
    values = _price_from_row(row)
    return dict(values, tenant_id=row["tenant_id"], updated_at=row["updated_at"])


def get_tenant_price(tenant_id):
    """读取某租户的默认单价；没设置过返回 None。"""
    if not tenant_id:
        return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tenant_price WHERE tenant_id=?", (tenant_id,))
        row = cur.fetchone()
        return _row_to_tenant_price(row) if row else None
    finally:
        conn.close()


def set_tenant_price(tenant_id, bw=None, color=None,
                     bw_duplex=None, color_duplex=None):
    """设置某租户的默认单价；只传部分字段时其余保持原值。

    返回该租户最新默认价字典。
    """
    incoming = {"bw": bw, "color": color,
                "bw_duplex": bw_duplex, "color_duplex": color_duplex}
    parsed = {key: _to_float(value) for key, value in incoming.items()}
    current = get_tenant_price(tenant_id) or {key: None for key, _ in _PRICE_FIELDS}
    merged = {key: (parsed[key] if parsed[key] is not None else current.get(key))
              for key, _ in _PRICE_FIELDS}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO tenant_price"
            " (tenant_id, bw_price, color_price, bw_duplex_price,"
            "  color_duplex_price, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, merged["bw"], merged["color"], merged["bw_duplex"],
             merged["color_duplex"], _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_tenant_price(tenant_id)


def clear_tenant_price(tenant_id):
    """清除某租户的默认价，使该租户恢复跟随平台全局价。返回是否有记录被删。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tenant_price WHERE tenant_id=?", (tenant_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def effective_price(device_id=None):
    """计算实际生效单价：设备独立价 > 租户默认价 > 平台全局价。

    层级：单台机器设了 device_price 用设备价；否则用该机所属租户的 tenant_price；
    都没有则跟随全局 price_config。
    返回 {device_id, tenant_id, bw, color, bw_duplex, color_duplex, source}，
    单面价单位为元/页，双面价单位为元/张；
    source 为 "device"（该机独立价）/ "tenant"（租户默认价）/ "global"（平台全局价）。
    """
    base = get_price_config()
    merged = {key: base[key] for key, _ in _PRICE_FIELDS}
    source = "global"
    tenant_id = None
    if device_id:
        device = get_device(device_id)
        tenant_id = (device or {}).get("tenant_id")
    if tenant_id:
        tenant_price = get_tenant_price(tenant_id)
        if tenant_price and any(tenant_price.get(key) is not None
                                for key, _ in _PRICE_FIELDS):
            for key, _ in _PRICE_FIELDS:
                if tenant_price.get(key) is not None:
                    merged[key] = tenant_price[key]
            source = "tenant"
    custom = get_device_price(device_id)
    if custom:
        for key, _ in _PRICE_FIELDS:
            if custom.get(key) is not None:
                merged[key] = custom[key]
        source = "device"
    return dict(merged, device_id=device_id, tenant_id=tenant_id, source=source)


# ----------------------- SaaS：订阅生效 / 营收 / 结算 -----------------------

def _tenant_deadline(expires_at):
    """订阅到期时间：unix 秒/毫秒或日期字符串；空值或不限期的非法值返回 None。"""
    if not expires_at:
        return None
    try:
        value = float(expires_at)
        if value > 1e12:  # 毫秒时间戳
            value = value / 1000.0
        return datetime.fromtimestamp(value)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(expires_at), fmt)
        except ValueError:
            continue
    return None


def tenant_service_state(tenant_id=None):
    """租户当前是否可对外提供服务，以及设备配额。

    停用 / 订阅停用 / 订阅到期都会让 active=False；默认租户不设上限。
    printer_limit<=0 或空表示不限量。
    """
    tid = (tenant_id or "default").strip() or "default"
    tenant = get_tenant(tid)
    devices = len(list_devices(tid)) if tenant else 0
    if tenant is None:
        return {
            "tenant_id": tid, "name": tid, "active": False,
            "reason": "租户不存在", "tenant_status": None,
            "subscription_status": None, "expires_at": None,
            "days_left": None, "device_count": devices,
            "printer_limit": None, "can_add_device": False,
            "fee_rate": DEFAULT_FEE_RATE,
        }
    subscription = tenant.get("subscription") or {}
    limit_raw = _to_int(subscription.get("printer_limit"))
    limit = limit_raw if limit_raw and limit_raw > 0 else None
    deadline = _tenant_deadline(subscription.get("expires_at"))
    now = datetime.now()
    days_left = None
    if deadline is not None:
        days_left = max(0, (deadline - now).days) if deadline > now else 0
    reasons = []
    if tenant.get("status") != "active":
        reasons.append("租户已被停用")
    if subscription.get("status") and subscription["status"] != "active":
        reasons.append("订阅已停用")
    if deadline is not None and now > deadline:
        reasons.append("订阅已到期")
    active = not reasons
    return {
        "tenant_id": tid,
        "name": tenant.get("name") or tid,
        "active": active,
        "reason": "；".join(reasons),
        "tenant_status": tenant.get("status"),
        "subscription_status": subscription.get("status"),
        "expires_at": subscription.get("expires_at"),
        "days_left": days_left,
        "device_count": devices,
        "printer_limit": limit,
        "can_add_device": bool(active and (limit is None or devices < limit)),
        "fee_rate": _fee_rate(subscription.get("fee_rate")),
    }


def tenant_revenue(tenant_id, start=None, end=None, unsettled_only=False):
    """已付款订单的流水（退款/超时免单不计）。

    start/end 为 "YYYY-MM-DD HH:MM:SS" 字符串，左闭右开；只统计写入了
    out_trade_no 的订单（即真实完成支付的订单）。
    """
    tid = (tenant_id or "default").strip() or "default"
    sql = ("SELECT COUNT(*) AS orders, COALESCE(SUM(amount), 0) AS amount"
           " FROM print_jobs WHERE tenant_id=?"
           " AND out_trade_no IS NOT NULL AND out_trade_no<>''"
           " AND COALESCE(waived, 0)=0")
    args = [tid]
    if start:
        sql += " AND created_at>=?"
        args.append(start)
    if end:
        sql += " AND created_at<?"
        args.append(end)
    if unsettled_only:
        sql += " AND COALESCE(settled, 0)=0"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        row = cur.fetchone()
        return {"orders": int(row["orders"] or 0),
                "amount": round(float(row["amount"] or 0), 2)}
    finally:
        conn.close()


def tenant_revenue_summary(tenant_id):
    """租户营收概览：今日 / 本月 / 累计 / 未结算，以及平台抽成与应结金额。"""
    state = tenant_service_state(tenant_id)
    now = datetime.now()
    today_start = now.strftime("%Y-%m-%d 00:00:00")
    month_start = now.strftime("%Y-%m-01 00:00:00")
    today = tenant_revenue(tenant_id, start=today_start)
    month = tenant_revenue(tenant_id, start=month_start)
    total = tenant_revenue(tenant_id)
    unsettled = tenant_revenue(tenant_id, unsettled_only=True)
    rate = state["fee_rate"]
    fee_total = round(total["amount"] * rate, 2)
    fee_unsettled = round(unsettled["amount"] * rate, 2)
    return {
        "tenant_id": state["tenant_id"],
        "today": today,
        "month": month,
        "total": total,
        "fee_rate": rate,
        "fee_total": fee_total,
        "net_total": round(total["amount"] - fee_total, 2),
        "unsettled": dict(unsettled,
                          fee=fee_unsettled,
                          net=round(unsettled["amount"] - fee_unsettled, 2)),
        "settlements": list_tenant_settlements(state["tenant_id"]),
    }


def list_tenant_settlements(tenant_id, limit=50):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tenant_settlements WHERE tenant_id=?"
            " ORDER BY id DESC LIMIT ?", ((tenant_id or "default"), int(limit)))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def settle_tenant(tenant_id, operator="", note=""):
    """把该租户所有「已付款且未结算」的订单打成一个结算批次。

    平台统一收款时，这里只做记账：amount 为租户流水，fee 为平台抽成，
    net 为应结给租户的金额；真实打款仍由线下完成。
    """
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return None
    state = tenant_service_state(tenant_id)
    rate = state["fee_rate"]
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, amount, created_at FROM print_jobs"
            " WHERE tenant_id=? AND out_trade_no IS NOT NULL AND out_trade_no<>''"
            " AND COALESCE(waived, 0)=0 AND COALESCE(settled, 0)=0"
            " ORDER BY id ASC", (tenant_id,))
        rows = cur.fetchall()
        if not rows:
            return {"orders": 0, "amount": 0.0, "fee_rate": rate,
                    "fee": 0.0, "net": 0.0, "settled_at": None}
        amount = round(sum(float(row["amount"] or 0) for row in rows), 2)
        fee = round(amount * rate, 2)
        net = round(amount - fee, 2)
        ids = [int(row["id"]) for row in rows]
        cur.execute(
            "INSERT INTO tenant_settlements"
            " (tenant_id, orders, amount, fee_rate, fee, net,"
            " period_start, period_end, settled_at, operator, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, len(ids), amount, rate, fee, net,
             rows[0]["created_at"], rows[-1]["created_at"], _now(),
             operator or "平台方", note or ""))
        settlement_id = cur.lastrowid
        cur.executemany("UPDATE print_jobs SET settled=1 WHERE id=?",
                        [(i,) for i in ids])
        conn.commit()
    finally:
        conn.close()
    record = {"id": settlement_id, "tenant_id": tenant_id, "orders": len(ids),
              "amount": amount, "fee_rate": rate, "fee": fee, "net": net,
              "period_start": rows[0]["created_at"],
              "period_end": rows[-1]["created_at"], "settled_at": _now(),
              "operator": operator or "平台方", "note": note or ""}
    return record


def list_device_prices():
    """列出全部设备及其生效单价，供管理端「每台机器定价」表格展示。

    每项含 device_id / name / bw / color / bw_duplex / color_duplex /
    source / updated_at，source 标明该价来自该机独立设置还是全局默认。
    """
    result = []
    for device in list_devices():
        device_id = device.get("device_id")
        price = effective_price(device_id)
        override = get_device_price(device_id)
        result.append({
            "device_id": device_id,
            "name": device.get("name"),
            "status": device.get("status"),
            "bw": price["bw"],
            "color": price["color"],
            "bw_duplex": price["bw_duplex"],
            "color_duplex": price["color_duplex"],
            "source": price["source"],
            "updated_at": override.get("updated_at") if override else None,
        })
    return result


# ----------------------- 收款配置 pay_config -----------------------
# 单行配置（id=1）。当前收款方式为「个人微信收款码」，没有商户号，
# 因此到账无法由后端自动确认：用户扫码付款后自己点「我已支付」，
# 流水记为「待核验」，由管理端在账单流水页人工确认收款。

def get_pay_config():
    """读取收款配置；没配置过时返回 setting 里的默认值。"""
    default = {"wechat_qr": setting.WECHAT_QR_URL, "payee": setting.WECHAT_PAYEE}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pay_config WHERE id=1")
        row = cur.fetchone()
        if row is None:
            return dict(default, updated_at=None)
        return {
            "wechat_qr": row["wechat_qr"] or default["wechat_qr"],
            "payee": row["payee"] if row["payee"] is not None else default["payee"],
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()


def set_pay_config(wechat_qr=None, payee=None):
    """管理端修改收款码路径 / 收款人昵称，返回最新配置。

    只传其中一项时另一项保持原值；两项都为空时返回现状不改动。
    """
    current = get_pay_config()
    new_qr = (wechat_qr or "").strip() if wechat_qr is not None else current["wechat_qr"]
    if wechat_qr is not None and not new_qr:
        new_qr = setting.WECHAT_QR_URL
    new_payee = payee if payee is not None else current["payee"]
    if wechat_qr is None and payee is None:
        return current
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO pay_config (id, wechat_qr, payee, updated_at)"
            " VALUES (1, ?, ?, ?)",
            (new_qr, new_payee, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_pay_config()


# ----------------------- 收款流水 payments -----------------------
# 状态口径（如实，不编造）：
#   submitted —— 用户声明已付，等待管理端核对到账；
#   confirmed —— 管理端已核对收到款。

def _row_to_payment(row):
    return {
        "id": row["id"],
        "out_trade_no": row["out_trade_no"],
        "device_id": row["device_id"],
        "amount": _to_float(row["amount"]) or 0.0,
        "method": row["method"],
        "status": row["status"],
        "detail": row["detail"],
        "pages": row["pages"],
        "sheets": row["sheets"],
        "payer_note": row["payer_note"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
    }


def add_payment(out_trade_no, amount, device_id=None, method="wechat_personal",
                status="submitted", detail="", pages=None, sheets=None,
                payer_note=None):
    """写入一条收款流水，返回流水 id；同一订单号重复写入时返回已有记录 id。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM payments WHERE out_trade_no=?", (out_trade_no,))
        row = cur.fetchone()
        if row is not None:
            return row["id"]
        cur.execute(
            """
            INSERT INTO payments
                (out_trade_no, device_id, amount, method, status, detail,
                 pages, sheets, payer_note, created_at, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (out_trade_no, device_id, _to_float(amount), method, status, detail,
             _to_int(pages), _to_int(sheets), payer_note, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_payments(limit=None):
    """列出收款流水，最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        sql = "SELECT * FROM payments ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        return [_row_to_payment(row) for row in cur.fetchall()]
    finally:
        conn.close()


def confirm_payment(payment_id):
    """把一条「待核验」流水标记为「已收款」，返回是否真的发生了变更。

    只允许 submitted → confirmed；已免单（waived）或待退款（refund_pending）
    的流水不再确认收款，避免把「已经免单的单」又标记成已收款。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE payments SET status='confirmed', confirmed_at=?"
            " WHERE id=? AND status='submitted'",
            (_now(), payment_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def payment_summary():
    """收款汇总：区分「已核验到账」「用户声明已付、待核验」「超时免收/待退款」。"""
    today = datetime.now().strftime("%Y-%m-%d")
    payments = list_payments()
    confirmed = [p for p in payments if p["status"] == "confirmed"]
    submitted = [p for p in payments if p["status"] == "submitted"]
    waived = [p for p in payments if p["status"] == "waived"]
    refund_pending = [p for p in payments if p["status"] == "refund_pending"]
    return {
        "count_total": len(payments),
        "count_confirmed": len(confirmed),
        "count_submitted": len(submitted),
        "count_waived": len(waived),
        "count_refund_pending": len(refund_pending),
        "confirmed_amount": round(sum(p["amount"] for p in confirmed), 2),
        "submitted_amount": round(sum(p["amount"] for p in submitted), 2),
        "waived_amount": round(sum(p["amount"] for p in waived), 2),
        "refund_pending_amount": round(sum(p["amount"] for p in refund_pending), 2),
        "confirmed_amount_today": round(
            sum(p["amount"] for p in confirmed
                if (p["confirmed_at"] or "").startswith(today)), 2),
        "submitted_amount_today": round(
            sum(p["amount"] for p in submitted
                if (p["created_at"] or "").startswith(today)), 2),
    }


# ----------------------- 未支付账单 bills（落库，重启不丢） -----------------------

def save_bill(out_trade_no, bill):
    """保存一张未支付账单（同号覆盖）。payload 以 JSON 存，避免进程重启丢失。"""
    payload = json.dumps(bill, ensure_ascii=False)
    expiry = float(bill.get("expiry") or 0)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bills (out_trade_no, payload, expiry, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(out_trade_no) DO UPDATE SET"
            " payload=excluded.payload, expiry=excluded.expiry, created_at=excluded.created_at",
            (out_trade_no, payload, expiry, _now()))
        conn.commit()
        return out_trade_no
    finally:
        conn.close()


def get_bill(out_trade_no):
    """取一张未过期账单；过期则顺手删除并返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bills WHERE out_trade_no=?", (out_trade_no,))
        row = cur.fetchone()
        if row is None:
            return None
        if float(row["expiry"] or 0) <= datetime.now().timestamp():
            cur.execute("DELETE FROM bills WHERE out_trade_no=?", (out_trade_no,))
            conn.commit()
            return None
        try:
            return json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return None
    finally:
        conn.close()


def delete_bill(out_trade_no):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM bills WHERE out_trade_no=?", (out_trade_no,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add_compliance_report(filename, result):
    """写入一条印前合规审核报告（同一文件保留历史，读取时取最新一条）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO compliance_reports (filename, verdict, categories, reason,"
            " model, pages, page_count, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, result.get("verdict"), json.dumps(result.get("categories") or [],
                                                         ensure_ascii=False),
             result.get("reason"), result.get("model"), int(result.get("pages") or 0),
             int(result.get("page_count") or 0),
             json.dumps(result.get("detail") or [], ensure_ascii=False), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_compliance_report(filename):
    """取某文件最新一条审核报告，没有则返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM compliance_reports WHERE filename = ?"
                    " ORDER BY id DESC LIMIT 1", (filename,))
        row = cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ("categories", "detail"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except (TypeError, ValueError):
                data[key] = []
        return data
    finally:
        conn.close()


def list_compliance_reports(limit=100):
    """管理端列表：最近 N 条审核报告。"""
    limit = max(1, min(int(limit or 100), 500))
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM compliance_reports ORDER BY id DESC LIMIT ?", (limit,))
        rows = []
        for row in cur.fetchall():
            data = dict(row)
            for key in ("categories", "detail"):
                try:
                    data[key] = json.loads(data.get(key) or "[]")
                except (TypeError, ValueError):
                    data[key] = []
            rows.append(data)
        return rows
    finally:
        conn.close()


def list_bills():
    """未过期账单列表 [(out_trade_no, bill), ...]，最新在前。"""
    now = datetime.now().timestamp()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bills WHERE expiry > ? ORDER BY rowid DESC", (now,))
        result = []
        for row in cur.fetchall():
            try:
                result.append((row["out_trade_no"], json.loads(row["payload"] or "{}")))
            except (TypeError, ValueError):
                continue
        return result
    finally:
        conn.close()


def clear_expired_bills():
    """删除过期账单，返回删除条数。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM bills WHERE expiry <= ?",
                    (datetime.now().timestamp(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ----------------------- 用户账号 users / 短信验证码 -----------------------

def _row_to_user(row):
    """数据库行 → 用户字典（不返回任何密码/验证码字段）。"""
    return {
        "id": row["id"],
        "phone": row["phone"],
        "nickname": row["nickname"],
        # 只告诉前端「是否已设置密码」，绝不返回哈希本身
        "has_password": bool(row["password_hash"]
                             if "password_hash" in row.keys() else False),
        "status": row["status"] or "active",
        "ban_reason": row["ban_reason"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def create_user(phone, nickname=None, password_hash=None):
    """创建账号，返回用户字典；手机号已存在时返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO users"
                " (phone, nickname, password_hash, status, created_at, last_login_at)"
                " VALUES (?, ?, ?, 'active', ?, NULL)",
                (phone, (nickname or "").strip() or None, password_hash, _now()))
        except sqlite3.IntegrityError:
            return None
        conn.commit()
        return get_user(cur.lastrowid)
    finally:
        conn.close()


def get_user_auth(phone):
    """登录鉴权专用：返回含 password_hash 的内部字典，对前端不可见。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE phone=?", (phone,))
        row = cur.fetchone()
        if row is None:
            return None
        data = _row_to_user(row)
        data["password_hash"] = (row["password_hash"]
                                 if "password_hash" in row.keys() else None)
        return data
    finally:
        conn.close()


def set_user_password(user_id, password_hash):
    """写入/更新密码哈希。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET password_hash=? WHERE id=?",
                    (password_hash, int(user_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user(user_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def get_user_by_phone(phone):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE phone=?", (phone,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def list_users(limit=500):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit), 2000)),))
        return [_row_to_user(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_user_status(user_id, status, reason=None):
    """封禁/解封账号；返回更新后的用户，账号不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET status=?, ban_reason=? WHERE id=?",
                    (status, reason, user_id))
        conn.commit()
        return get_user(user_id) if cur.rowcount else None
    finally:
        conn.close()


def touch_user_login(user_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), user_id))
        conn.commit()
    finally:
        conn.close()


def update_user_nickname(user_id, nickname):
    """修改昵称；账号不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET nickname=? WHERE id=?", (nickname, user_id))
        conn.commit()
        return get_user(user_id) if cur.rowcount else None
    finally:
        conn.close()


def count_recent_sms(phone, seconds=3600):
    """统计手机号在最近 N 秒内请求验证码的次数（小时级配额用）。"""
    since = (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM sms_codes WHERE phone=? AND created_at>=?",
            (phone, since))
        return int(cur.fetchone()["n"] or 0)
    finally:
        conn.close()


def save_sms_code(phone, code_hash, expires_at):
    """保存一条验证码（同手机号旧码全部作废），返回新记录 id。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE sms_codes SET used=1 WHERE phone=? AND used=0", (phone,))
        cur.execute(
            "INSERT INTO sms_codes (phone, code_hash, expires_at, attempts, used,"
            " created_at) VALUES (?, ?, ?, 0, 0, ?)",
            (phone, code_hash, float(expires_at), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_latest_sms_code(phone):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM sms_codes WHERE phone=? AND used=0"
            " ORDER BY id DESC LIMIT 1", (phone,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def bump_sms_attempt(code_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE sms_codes SET attempts=COALESCE(attempts,0)+1 WHERE id=?",
                    (code_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def mark_sms_used(code_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE sms_codes SET used=1 WHERE id=?", (code_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_jobs_by_user(user_id):
    """登录用户的历史订单（跨设备），最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM print_jobs WHERE user_id=? ORDER BY id DESC",
                    (user_id,))
        return [_row_to_job(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ----------------------- 告警 alerts -----------------------

def _row_to_alert(row):
    """把数据库行转成告警字典，resolved 字段表示是否已处理。"""
    alert = dict(row)
    alert["resolved"] = alert.get("resolved_at") is not None
    return alert


def add_alert(rule, device_id, level, message, triggered_at=None):
    """新增一条告警，返回新记录 id。

    告警按设备归属的租户打标（device 无租户时归 default），
    便于租户只能看到自己设备的告警。
    """
    tenant_id = "default"
    if device_id:
        device = get_device(device_id)
        if device and device.get("tenant_id"):
            tenant_id = device["tenant_id"]
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alerts (rule, device_id, level, message, triggered_at,
                                resolved_at, push_status, tenant_id)
            VALUES (?, ?, ?, ?, ?, NULL, 'pending', ?)
            """,
            (rule, device_id, level, message, triggered_at or _now(), tenant_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_alerts(resolved=None, tenant_id=None):
    """列出告警。resolved 为 None 时全部；True 只看已处理；False 只看未处理。

    传 tenant_id 时只返回该租户设备的告警。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        clauses = []
        params = []
        if resolved is True:
            clauses.append("resolved_at IS NOT NULL")
        elif resolved is False:
            clauses.append("resolved_at IS NULL")
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(f"SELECT * FROM alerts{where} ORDER BY id DESC", params)
        return [_row_to_alert(row) for row in cur.fetchall()]
    finally:
        conn.close()


def mark_alert_push(alert_id, ok, detail=""):
    """记录一次推送结果：成功置 pushed，失败置 failed 并累加尝试次数。"""
    status = "pushed" if ok else "failed"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE alerts SET push_status=?, pushed_at=?,"
            " push_attempts=COALESCE(push_attempts,0)+1, push_detail=?"
            " WHERE id=?",
            (status, _now(), (detail or "")[:300], alert_id))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_alerts_needing_push():
    """未处理且尚未成功推送的告警（启动补发用）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM alerts WHERE resolved_at IS NULL"
            " AND COALESCE(push_status,'pending') != 'pushed' ORDER BY id ASC")
        return [_row_to_alert(row) for row in cur.fetchall()]
    finally:
        conn.close()


def resolve_alert(alert_id):
    """把告警标记为已处理，返回被更新的记录数。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE alerts SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
            (_now(), alert_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def resolve_device_alerts_except(device_id, keep_rules):
    """自动关窗：把该设备名下、规则不在 keep_rules 中的未处理告警标记为已处理。

    供告警引擎使用——当某条规则的条件已经恢复（例如设备重新上报心跳）时，
    把之前那条未处理告警自动关闭，避免告警一直挂着不消失。
    keep_rules 为当前仍然成立的规则集合（例如 {"R1", "R2"}）。
    返回被关闭的告警条数。
    """
    keep = [rule for rule in (keep_rules or []) if rule]
    conn = _connect()
    try:
        cur = conn.cursor()
        if keep:
            placeholders = ",".join("?" for _ in keep)
            sql = (
                "UPDATE alerts SET resolved_at=? WHERE device_id=? "
                f"AND resolved_at IS NULL AND rule NOT IN ({placeholders})"
            )
            params = [_now(), device_id] + keep
        else:
            sql = (
                "UPDATE alerts SET resolved_at=? "
                "WHERE device_id=? AND resolved_at IS NULL"
            )
            params = [_now(), device_id]
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def is_alert_open(rule, device_id):
    """判断某规则+设备是否存在未处理告警，用于去重。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM alerts
            WHERE rule=? AND device_id=? AND resolved_at IS NULL
            LIMIT 1
            """,
            (rule, device_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


# ----------------------- 团队协作任务 tasks -----------------------
# 独立协作看板的数据层。字段与设计稿一致：任务 / 状态 / 优先级 / 团队(模块) /
# 进度 / 指派人 / 标签 / 关注 / 最后更新。

TASK_STATUSES = ("未开始", "进行中", "待验收", "已完成", "已搁置")
TASK_PRIORITIES = ("P0", "P1", "P2", "P3")
TASK_TEAMS = ("用户端", "管理端", "打印端", "后端服务", "硬件", "项目")


def _row_to_task(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "team": row["team"],
        "status": row["status"],
        "priority": row["priority"],
        "progress": row["progress"],
        "assignee": row["assignee"] or "",
        "tags": row["tags"] or "",
        "note": row["note"] or "",
        "starred": bool(row["starred"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_tasks():
    """返回全部任务，最近更新的排前面。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks ORDER BY updated_at DESC, id DESC")
        return [_row_to_task(row) for row in cur.fetchall()]
    finally:
        conn.close()


def add_task(title, team="用户端", status="未开始", priority="P2", progress=0,
             assignee="", tags="", note="", starred=0):
    """新增任务，返回任务字典。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        now = _now()
        cur.execute(
            """
            INSERT INTO tasks
                (title, team, status, priority, progress, assignee, tags, note,
                 starred, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, team, status, priority, max(0, min(_to_int(progress) or 0, 100)),
             assignee, tags, note, 1 if starred else 0, now, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,))
        return _row_to_task(cur.fetchone())
    finally:
        conn.close()


def update_task(task_id, **fields):
    """按字段局部更新任务，返回更新后的任务；任务不存在返回 None。"""
    allowed = {"title", "team", "status", "priority", "progress",
               "assignee", "tags", "note", "starred"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_task(task_id)
    if "progress" in updates:
        updates["progress"] = max(0, min(_to_int(updates["progress"]) or 0, 100))
    if "starred" in updates:
        updates["starred"] = 1 if updates["starred"] else 0
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key}=?" for key in updates)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE tasks SET {assignments} WHERE id=?",
                    list(updates.values()) + [task_id])
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_task(task_id)


def get_task(task_id):
    """按 id 查询任务，不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        return _row_to_task(row) if row else None
    finally:
        conn.close()


def delete_task(task_id):
    """删除任务，返回是否删掉了记录。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ----------------------- 拼单打印 print_groups / group_members -----------------------

def create_group(group_no, owner_name, owner_key, device_id=None, pay_mode="aa",
                 owner_uid=None):
    """新建拼单，返回记录 id。group_no 是分享给同学的 6 位短码。

    pay_mode: aa=每人支付自己的份额；unified=发起人统一付款。
    owner_uid 记录发起人账号：重新登录/换设备后仍能认出发起人。
    """
    mode = pay_mode if pay_mode in ("aa", "unified") else "aa"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO print_groups"
            " (group_no, owner_name, owner_key, owner_uid, device_id, status,"
            "  pay_mode, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            (group_no, owner_name, owner_key, _to_int(owner_uid), device_id,
             mode, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row_to_group(row):
    return {
        "group_no": row["group_no"],
        "owner_name": row["owner_name"],
        "owner_key": row["owner_key"],
        "owner_uid": (row["owner_uid"] if "owner_uid" in row.keys() else None),
        "device_id": row["device_id"],
        "status": row["status"],
        "discount": _to_float(row["discount"]),
        "total_price": _to_float(row["total_price"]),
        "order_no": row["order_no"],
        "pickup_code": row["pickup_code"],
        "pay_mode": (row["pay_mode"] if "pay_mode" in row.keys() else None) or "unified",
        "created_at": row["created_at"],
        "settled_at": row["settled_at"],
    }


def get_group(group_no):
    """按短码查拼单，不存在返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM print_groups WHERE group_no=?", (group_no,))
        row = cur.fetchone()
        return _row_to_group(row) if row else None
    finally:
        conn.close()


def update_group(group_no, **fields):
    """局部更新拼单（状态 / 折扣 / 总价 / 订单号 / 取件码）。"""
    allowed = {"status", "discount", "total_price", "order_no",
               "pickup_code", "device_id", "settled_at"}
    keys = [k for k in fields if k in allowed]
    if not keys:
        return 0
    assignments = ", ".join(f"{k}=?" for k in keys)
    values = [fields[k] for k in keys] + [group_no]
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE print_groups SET {assignments} WHERE group_no=?",
                    values)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _row_to_member(row):
    return {
        "id": row["id"],
        "group_no": row["group_no"],
        "name": row["name"],
        "folder": row["folder"],
        "filename": row["filename"],
        "print_copies": _to_int(row["print_copies"]) or 1,
        "print_range_start": _to_int(row["print_range_start"]) or 1,
        "print_range_end": _to_int(row["print_range_end"]) or 1,
        "print_side": row["print_side"] or "one-sided",
        "print_color": row["print_color"] or "bw",
        "paper": row["paper"] or "A4",
        "skip_blank": bool(row["skip_blank"]) if row["skip_blank"] is not None else True,
        "pages": _to_int(row["pages"]) or 0,
        "amount": _to_float(row["amount"]) or 0.0,
        "saved_amount": _to_float(row["saved_amount"]) or 0.0,
        "blank_pages": _load_batch_names(row["blank_pages"]),
        "user_id": (_to_int(row["user_id"]) if "user_id" in row.keys() else None),
        "payable": (_to_float(row["payable"]) if "payable" in row.keys() else None),
        "paid": bool(row["paid"]) if "paid" in row.keys() and row["paid"] else False,
        "out_trade_no": (row["out_trade_no"] if "out_trade_no" in row.keys() else None),
        "paid_at": (row["paid_at"] if "paid_at" in row.keys() else None),
        "pickup_code": (row["pickup_code"] if "pickup_code" in row.keys() else None),
        "created_at": row["created_at"],
    }


def add_group_member(group_no, name, folder, filename, copies, start, end,
                     side, color, paper, skip_blank, pages, amount,
                     saved_amount, blank_pages, user_id=None):
    """把一个同学的文件加进拼单，返回成员 id。

    同一个会话目录重复加入时覆盖旧记录：否则成员数、页数和金额都会翻倍。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM group_members WHERE group_no=? AND folder=?",
                    (group_no, folder))
        cur.execute(
            """
            INSERT INTO group_members
                (group_no, name, folder, filename, print_copies,
                 print_range_start, print_range_end, print_side, print_color,
                 paper, skip_blank, pages, amount, saved_amount, blank_pages,
                 user_id, paid, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (group_no, name, folder, filename, _to_int(copies),
             _to_int(start), _to_int(end), side, color, paper,
             _to_bool_int(bool(skip_blank)), _to_int(pages),
             _to_float(amount), _to_float(saved_amount),
             json.dumps(list(blank_pages or []), ensure_ascii=False),
             _to_int(user_id), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_group_members(group_no):
    """拼单成员列表，按加入顺序（最早的在前）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM group_members WHERE group_no=? ORDER BY id ASC",
            (group_no,))
        return [_row_to_member(row) for row in cur.fetchall()]
    finally:
        conn.close()


def delete_group_member(member_id):
    """移除一名成员（本人退出或发起人删掉），返回是否删掉。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM group_members WHERE id=?", (int(member_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_group_members_by_folder(group_no, folder):
    """退出拼单：删掉该会话目录下加入的全部成员，返回被删的成员列表。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM group_members WHERE group_no=? AND folder=?",
            (group_no, folder))
        members = [_row_to_member(row) for row in cur.fetchall()]
        cur.execute("DELETE FROM group_members WHERE group_no=? AND folder=?",
                    (group_no, folder))
        conn.commit()
        return members
    finally:
        conn.close()


def delete_group_members_by_user(group_no, user_id):
    """退出/重复加入：按登录账号删除成员，返回被删成员列表。"""
    if not user_id:
        return []
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM group_members WHERE group_no=? AND user_id=?",
            (group_no, int(user_id)))
        members = [_row_to_member(row) for row in cur.fetchall()]
        cur.execute(
            "DELETE FROM group_members WHERE group_no=? AND user_id=?",
            (group_no, int(user_id)))
        conn.commit()
        return members
    finally:
        conn.close()


def set_group_member_bill(member_id, payable, out_trade_no, pickup_code):
    """AA 结算：写入某成员的应付金额、账单号与取件码，并重置为未付款。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE group_members SET payable=?, out_trade_no=?, pickup_code=?,"
            " paid=0, paid_at=NULL WHERE id=?",
            (_to_float(payable), out_trade_no, pickup_code, int(member_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_group_member_paid(out_trade_no):
    """成员支付成功后标记已付款，返回所属拼单短码（找不到返回 None）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT group_no FROM group_members WHERE out_trade_no=?",
            (out_trade_no,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE group_members SET paid=1, paid_at=?"
            " WHERE out_trade_no=? AND (paid IS NULL OR paid=0)",
            (_now(), out_trade_no))
        conn.commit()
        return row["group_no"]
    finally:
        conn.close()


def refresh_group_paid_status(group_no):
    """所有人付完把拼单标为 paid，否则保持 collecting。返回最新状态。"""
    members = list_group_members(group_no)
    if not members:
        return None
    status = "paid" if all(m["paid"] for m in members) else "collecting"
    if status == "paid":
        # 全部成员付完才写结算时间：清理任务据此等 24 小时再删目录
        update_group(group_no, status=status, settled_at=_now())
    else:
        update_group(group_no, status=status)
    return status


def claim_group_for_collect(group_no):
    """原子地把 AA 拼单从 open 切到 collecting。

    返回 True 表示本次调用抢到了锁定权；并发/重复点击时只有一次成功，
    避免生成两套成员账单导致重复付款。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE print_groups SET status='collecting'"
            " WHERE group_no=? AND status='open'"
            " AND COALESCE(pay_mode,'unified')='aa'", (group_no,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reset_group_member_bills(group_no):
    """取消 AA 收款：清空成员的应付/账单/取件信息（仅未付款时调用）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE group_members SET payable=NULL, out_trade_no=NULL,"
            " pickup_code=NULL, paid=0, paid_at=NULL WHERE group_no=?",
            (group_no,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_open_groups(limit=50):
    """进行中的拼单（管理端/调试用），最新在前。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM print_groups WHERE status='open'"
            " ORDER BY id DESC LIMIT ?", (int(limit),))
        return [_row_to_group(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ----------------------- 资料库分类 library_categories -----------------------

def list_library_categories():
    """按排序返回全部分类。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, sort_order FROM library_categories"
            " ORDER BY sort_order ASC, id ASC")
        return [{"id": row["id"], "name": row["name"],
                 "sort_order": _to_int(row["sort_order"]) or 0}
                for row in cur.fetchall()]
    finally:
        conn.close()


def add_library_category(name, sort_order=100):
    """新增分类；重名返回 None。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO library_categories"
            " (name, sort_order, created_at) VALUES (?, ?, ?)",
            (name, int(sort_order), _now()))
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        conn.close()


def delete_library_category(name):
    """删除分类，返回是否删除成功。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM library_categories WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_library_docs_by_category(name):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM library_docs WHERE category=?",
                    (name,))
        return int(cur.fetchone()["n"])
    finally:
        conn.close()


def category_exists(name):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM library_categories WHERE name=?", (name,))
        return cur.fetchone() is not None
    finally:
        conn.close()


# ----------------------- 共享资料库 library_docs -----------------------

def _row_to_library_doc(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"] or "其他",
        "pages": _to_int(row["pages"]) or 0,
        "size_bytes": _to_int(row["size_bytes"]) or 0,
        "status": row["status"],
        "agent_verdict": row["agent_verdict"],
        "agent_reason": row["agent_reason"],
        "agent_model": row["agent_model"],
        "agent_at": row["agent_at"],
        "admin_note": row["admin_note"],
        "reviewer": row["reviewer"],
        "reviewed_at": row["reviewed_at"],
        "uploader_uid": _to_int(row["uploader_uid"]),
        "uploader_name": row["uploader_name"],
        "tenant_id": row["tenant_id"] or "default",
        "order_no": row["order_no"],
        "pending_path": row["pending_path"],
        "published_path": row["published_path"],
        "filename": row["filename"],
        "downloads": _to_int(row["downloads"]) or 0,
        "print_count": _to_int(
            row["print_count"] if "print_count" in row.keys() else 0) or 0,
        "print_pages": _to_int(
            row["print_pages"] if "print_pages" in row.keys() else 0) or 0,
        "created_at": row["created_at"],
        "published_at": row["published_at"],
    }


def add_library_doc(title, category, pages, size_bytes, pending_path,
                    uploader_uid=None, uploader_name="", tenant_id="default",
                    order_no=""):
    """新增一条待审资料，返回 id；文件已复制到 pending_path。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO library_docs
                (title, category, pages, size_bytes, status, uploader_uid,
                 uploader_name, tenant_id, order_no, pending_path, created_at)
            VALUES (?, ?, ?, ?, 'pending_agent', ?, ?, ?, ?, ?, ?)
            """,
            (title, category, _to_int(pages), _to_int(size_bytes),
             _to_int(uploader_uid), uploader_name, tenant_id, order_no,
             pending_path, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_library_doc(doc_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM library_docs WHERE id=?", (int(doc_id),))
        row = cur.fetchone()
        return _row_to_library_doc(row) if row else None
    finally:
        conn.close()


def list_library_docs(status=None, q="", limit=100, offset=0):
    """按状态/关键词查询资料；q 只匹配标题与分类。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        clauses, params = [], []
        if status:
            if isinstance(status, (list, tuple)):
                clauses.append("status IN (%s)" % ",".join("?" for _ in status))
                params.extend(status)
            else:
                clauses.append("status=?")
                params.append(status)
        if q:
            clauses.append("(title LIKE ? OR category LIKE ?)")
            like = "%" + q.strip() + "%"
            params.extend([like, like])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(
            f"SELECT * FROM library_docs{where}"
            " ORDER BY COALESCE(published_at, created_at) DESC, id DESC"
            " LIMIT ? OFFSET ?",
            params + [max(int(limit), 1), max(int(offset), 0)],
        )
        return [_row_to_library_doc(row) for row in cur.fetchall()]
    finally:
        conn.close()


def count_library_docs(status=None):
    conn = _connect()
    try:
        cur = conn.cursor()
        if status:
            cur.execute("SELECT COUNT(*) AS n FROM library_docs WHERE status=?",
                        (status,))
        else:
            cur.execute("SELECT COUNT(*) AS n FROM library_docs")
        return int(cur.fetchone()["n"])
    finally:
        conn.close()


def library_status_counts():
    """一次取回各状态数量，供管理端看板展示。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) AS n FROM library_docs GROUP BY status")
        return {row["status"]: int(row["n"]) for row in cur.fetchall()}
    finally:
        conn.close()


def set_library_agent_result(doc_id, verdict, reason, model=""):
    """agent 初审结果：block 直接拒绝，其余转管理员复审。

    未接入/调用失败也转人工，并如实记录 verdict=unavailable/unknown，
    绝不把「没审成」写成通过。
    """
    status = "rejected" if verdict == "block" else "pending_admin"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE library_docs SET status=?, agent_verdict=?, agent_reason=?,"
            " agent_model=?, agent_at=? WHERE id=?",
            (status, verdict, reason, model, _now(), int(doc_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_library_published(doc_id, filename, published_path, reviewer=""):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE library_docs SET status='published', filename=?,"
            " published_path=?, pending_path='', reviewer=?, reviewed_at=?,"
            " published_at=? WHERE id=?",
            (filename, published_path, reviewer, _now(), _now(), int(doc_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_library_rejected(doc_id, reason, reviewer=""):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE library_docs SET status='rejected', admin_note=?,"
            " reviewer=?, reviewed_at=?, pending_path='' WHERE id=?",
            (reason, reviewer, _now(), int(doc_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def bump_library_download(doc_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE library_docs SET downloads=COALESCE(downloads,0)+1 WHERE id=?",
            (int(doc_id),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add_library_print(doc_id, pages=0):
    """资料被真实打印一次：打印次数 +1，累计页数 += pages。

    计数发生在付费入队时（而不是下载/取用时），保证「打印量」不是虚的。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE library_docs SET print_count=COALESCE(print_count,0)+1,"
            " print_pages=COALESCE(print_pages,0)+? WHERE id=?",
            (max(int(pages or 0), 0), int(doc_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def library_contributor_stats(uid):
    """某个用户已公开资料的累计打印量（用于贡献折扣）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS docs,"
            " COALESCE(SUM(print_count),0) AS prints,"
            " COALESCE(SUM(print_pages),0) AS pages"
            " FROM library_docs"
            " WHERE status='published' AND uploader_uid=?",
            (int(uid),),
        )
        row = cur.fetchone()
        return {
            "docs": int(row["docs"] or 0),
            "print_count": int(row["prints"] or 0),
            "print_pages": int(row["pages"] or 0),
        }
    finally:
        conn.close()


def list_stale_library_pending(days=7):
    """超过 N 天仍未审完的待审资料（用于自动关闭，避免长期占用磁盘）。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM library_docs"
            " WHERE status IN ('pending_agent', 'pending_admin') AND created_at<?",
            (cutoff,),
        )
        return [_row_to_library_doc(row) for row in cur.fetchall()]
    finally:
        conn.close()


# 首次初始化时写入的真实功能清单（按模块树整理，状态与当前实现一致）
_TASK_SEED = [
    # ---- 用户端 ----
    ("用户端", "上传文件（文档/图片，多文件）", "已完成", "P1", 100, "青芷", "核心,已上线"),
    ("用户端", "选择打印机（在线/UPS/单价）", "已完成", "P0", 100, "青芷", "核心,已上线"),
    ("用户端", "单价与预估价展示", "已完成", "P1", 100, "青芷", "已上线"),
    ("用户端", "个人微信收款码 + 金额", "已完成", "P1", 100, "浩苏", "支付,已上线"),
    ("用户端", "「我已支付完成」提交", "已完成", "P1", 100, "浩苏", "支付,已上线"),
    ("用户端", "付款备注", "已完成", "P2", 100, "浩苏", "支付"),
    ("用户端", "打印范围（起止页）", "已完成", "P1", 100, "青芷", ""),
    ("用户端", "份数", "已完成", "P1", 100, "青芷", ""),
    ("用户端", "双面（单面/双面默认/长边/短边）", "已完成", "P1", 100, "青芷", ""),
    ("用户端", "文件预览（pdf.js）", "进行中", "P2", 30, "", "缺陷：/pdfjs 资源缺失"),
    ("用户端", "转换状态与失败提示", "已完成", "P2", 100, "", ""),
    ("用户端", "UPS 供电状态展示", "已完成", "P2", 100, "青芷", "硬件预留"),
    ("用户端", "纸张规格（A4/80g）", "未开始", "P2", 0, "青芷", ""),
    ("用户端", "色彩（黑白/彩色计费）", "未开始", "P2", 0, "青芷", "计价"),
    ("用户端", "页缩略图预览", "未开始", "P2", 0, "青芷", ""),
    ("用户端", "最近订单", "未开始", "P2", 0, "青芷", ""),
    ("用户端", "取件码", "未开始", "P1", 0, "青芷", "取件"),
    ("用户端", "已支付/打印中/待取件 步骤条", "未开始", "P2", 0, "青芷", "取件"),
    ("用户端", "打印进度（x/y 页）", "未开始", "P2", 0, "青芷", "取件"),
    ("用户端", "订单详情（订单号/规格/金额/时间）", "未开始", "P2", 0, "青芷", ""),
    ("用户端", "再次打印", "未开始", "P3", 0, "青芷", ""),
    ("用户端", "门店定位 / 距离 / 步行时间", "未开始", "P3", 0, "未分配", "多门店"),
    ("用户端", "指标条（均价/等待/24h）", "未开始", "P3", 0, "青芷", ""),
    ("用户端", "支付宝当面付", "未开始", "P1", 0, "浩苏", "支付"),
    ("用户端", "上传大小限制（100MB）", "未开始", "P3", 0, "青芷", ""),
    # ---- 管理端 ----
    ("管理端", "概览 KPI", "已完成", "P1", 100, "青芷", "已上线"),
    ("管理端", "设备列表", "已完成", "P0", 100, "青芷", "核心,已上线"),
    ("管理端", "新增 / 删除设备", "已完成", "P1", 100, "青芷", "已上线"),
    ("管理端", "设置纸盒容量与剩余", "已完成", "P1", 100, "青芷", "已上线"),
    ("管理端", "订单列表", "已完成", "P1", 100, "青芷", "已上线"),
    ("管理端", "告警列表 / 处理 / 自动关窗", "已完成", "P0", 100, "青芷", "核心,已上线"),
    ("管理端", "R1 纸量 + R2 离线规则", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("管理端", "R3~R5 规则（接口预留）", "待验收", "P1", 100, "", "待硬件触发"),
    ("管理端", "企微群机器人推送", "待验收", "P2", 100, "", "代码已测，线上待配置 Webhook"),
    ("管理端", "测试推送接口", "已完成", "P2", 100, "浩苏", "通知"),
    ("管理端", "账单流水（汇总/明细）", "已完成", "P1", 100, "青芷", "支付,已上线"),
    ("管理端", "收款码与收款人设置", "已完成", "P2", 100, "青芷", "支付"),
    ("管理端", "确认收款", "已完成", "P1", 100, "青芷", "支付"),
    ("管理端", "全局单价设置", "已完成", "P1", 100, "青芷", "计价"),
    ("管理端", "每台机器独立定价", "已完成", "P1", 100, "青芷", "计价,已上线"),
    ("管理端", "价格对比（vs PaperCut）", "已完成", "P2", 100, "青芷", ""),
    ("管理端", "接入状态页（已接入/待做）", "已完成", "P2", 100, "青芷", ""),
    ("管理端", "订单列表金额列", "未开始", "P1", 0, "青芷", ""),
    ("管理端", "订单 KPI（今日/已完成/待取件）", "未开始", "P2", 0, "青芷", ""),
    ("管理端", "订单搜索 / 筛选 / 分页", "未开始", "P2", 0, "青芷", ""),
    ("管理端", "导出 CSV", "未开始", "P2", 0, "青芷", ""),
    ("管理端", "概览实时设备表", "未开始", "P2", 0, "青芷", ""),
    ("管理端", "概览待处理告警卡", "未开始", "P2", 0, "青芷", ""),
    ("管理端", "今日打印页数趋势图", "未开始", "P3", 0, "青芷", ""),
    ("管理端", "设备详情页", "未开始", "P2", 0, "青芷", ""),
    ("管理端", "实时日志（CUPS）", "未开始", "P3", 0, "浩苏", ""),
    ("管理端", "重启设备 / 打印测试页", "未开始", "P3", 0, "浩苏", ""),
    ("管理端", "维护计划 / 记录维护", "未开始", "P3", 0, "未分配", ""),
    ("管理端", "告警规则开关 / 阈值可调", "未开始", "P3", 0, "浩苏", ""),
    ("管理端", "飞书 / 短信 / 邮件通知", "未开始", "P3", 0, "浩苏", "通知"),
    ("管理端", "值班信息 / 升级联系人", "未开始", "P3", 0, "未分配", ""),
    ("管理端", "导出日报", "未开始", "P3", 0, "青芷", ""),
    # ---- 打印端 ----
    ("打印端", "设备注册 + 25 秒心跳", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("打印端", "任务轮询 / 文件下载 / 状态回传", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("打印端", "Windows 打印（SumatraPDF）", "已完成", "P0", 100, "浩苏", "核心,实测出纸"),
    ("打印端", "图片订单文件名 / 页数修复", "已完成", "P0", 100, "浩苏", "缺陷修复"),
    ("打印端", "在线状态按心跳现算", "已完成", "P0", 100, "浩苏", "缺陷修复"),
    ("打印端", "Linux 打印（CUPS + ipptool）", "待验收", "P1", 100, "", "待现场验证"),
    ("打印端", "逐页 / 逐份打印进度上报", "未开始", "P2", 0, "浩苏", ""),
    ("打印端", "打印失败重试与告警", "未开始", "P1", 0, "浩苏", ""),
    ("打印端", "重启打印机 / 测试页指令通道", "未开始", "P3", 0, "浩苏", ""),
    # ---- 后端服务 ----
    ("后端服务", "device / print_jobs / alerts 三表", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("后端服务", "price_config / device_price", "已完成", "P1", 100, "浩苏", "计价"),
    ("后端服务", "pay_config / payments", "已完成", "P1", 100, "浩苏", "支付"),
    ("后端服务", "设备 API（注册/心跳/列表/设纸量）", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("后端服务", "打印队列与订单 API", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("后端服务", "按机生效价结算", "已完成", "P1", 100, "浩苏", "计价"),
    ("后端服务", "微信支付提交 / 流水 / 核验", "已完成", "P1", 100, "浩苏", "支付"),
    ("后端服务", "文档转换（LibreOffice / img2pdf）", "已完成", "P0", 100, "浩苏", "核心"),
    ("后端服务", "告警引擎（10 秒扫描）", "已完成", "P0", 100, "浩苏", "核心,已上线"),
    ("后端服务", "通知（企微 Webhook 异步）", "已完成", "P2", 100, "浩苏", "通知"),
    ("后端服务", "鉴权（JWT / SECRET_KEY）", "已完成", "P0", 100, "浩苏", "安全"),
    ("后端服务", "统计报表 / 导出", "未开始", "P3", 0, "浩苏", ""),
    ("后端服务", "多门店 / 多租户", "未开始", "P3", 0, "未分配", ""),
    # ---- 硬件 ----
    ("硬件", "ESP32-S3 接入", "未开始", "P1", 0, "未分配", "待硬件"),
    ("硬件", "UPS 接入（ups_online/runtime）", "未开始", "P2", 0, "未分配", "待硬件"),
    ("硬件", "门磁（R4）", "未开始", "P2", 0, "未分配", "待硬件"),
    ("硬件", "取件检测（R5）", "未开始", "P3", 0, "未分配", "待硬件"),
    ("硬件", "打印柜体（取件口/检修门/散热）", "未开始", "P2", 0, "未分配", "结构"),
    ("硬件", "真机 CUPS 现场验证", "未开始", "P0", 0, "", "现场待验证"),
    # ---- 项目（演示 / 客户） ----
    ("项目", "演示脚本与现场讲解", "未开始", "P1", 0, "", "演示"),
    ("项目", "客户试用与反馈收集", "未开始", "P2", 0, "", "客户"),
]


def seed_tasks():
    """表为空时写入真实功能清单；已有数据则不动（幂等）。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks")
        if cur.fetchone()[0]:
            return 0
        now = _now()
        cur.executemany(
            """
            INSERT INTO tasks
                (title, team, status, priority, progress, assignee, tags, note,
                 starred, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)
            """,
            [(title, team, status, priority, progress, _seed_assignee(team, title),
              tags, now, now)
             for team, title, status, priority, progress, _assignee, tags in _TASK_SEED],
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# 真实成员与分工：柏延峰(硬件)、蒋平(经理/策划/设计)、浩苏(软件开发)、
# qingzhi(硬件指导 + 工控机配置)。下面按模块与任务性质自动分配负责人。
TASK_MEMBERS = {
    "浩苏": "软件开发",
    "蒋平": "产品 · 商务 · 战略",
    "柏延峰": "演示 · 硬件 · 真机 · 客户",
    "qingzhi": "硬件决策 · 工控机配置",
}

# 任务归属：软件实现全部由浩苏负责；其余只保留决策/硬件/演示类。
# 蒋平：产品 / 商务 / 战略（只做决策与对外，不写代码）
_PRODUCT_TASKS = {
    "门店定位 / 距离 / 步行时间",
    "指标条（均价/等待/24h）",
    "色彩（黑白/彩色计费）",
    "再次打印",
    "上传大小限制（100MB）",
    "价格对比（vs PaperCut）",
    "导出日报",
    "值班信息 / 升级联系人",
}

# 柏延峰：演示、真机、客户、实体硬件
_DEMO_TASKS = {
    "打印柜体（取件口/检修门/散热）",
    "真机 CUPS 现场验证",
    "维护计划 / 记录维护",
    "演示脚本与现场讲解",
    "客户试用与反馈收集",
}

# qingzhi：硬件决策与接入、工控机上的打印环境配置
_QINGZHI_TASKS = {
    "ESP32-S3 接入",
    "UPS 接入（ups_online/runtime）",
    "门磁（R4）",
    "取件检测（R5）",
    "Linux 打印（CUPS + ipptool）",
}


def _seed_assignee(team, title):
    """按标题归属表分配负责人；表外任务（全部软件实现）统一归浩苏。"""
    if title in _PRODUCT_TASKS:
        return "蒋平"
    if title in _DEMO_TASKS:
        return "柏延峰"
    if title in _QINGZHI_TASKS:
        return "qingzhi"
    return "浩苏"
