import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta

import db
from global_vars.files_attributes_singleton import files_attributes_singleton

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_time(value):
    try:
        return datetime.strptime(value, TIME_FMT)
    except (TypeError, ValueError):
        return None


def _delete_file(path, folder, filename, order_no, reason):
    """删一个文件并留一条删除记录；文件已经不在时也照样记（幂等排查用）。"""
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        return False
    db.add_file_deletion(folder, filename, order_no, reason)
    return True


def _delete_folder(path, folder, order_no, reason):
    """按目录递归删除并逐文件留痕，最后删掉空目录。"""
    if not os.path.isdir(path):
        return 0
    deleted = 0
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path).replace("\\", "/")
            if _delete_file(full, folder, rel, order_no, reason):
                deleted += 1
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass
    return deleted


def startup():
    """启动时清理 uploads：未付款的临时上传/预览会话可安全丢弃。

    保留 pending_files：已付款、待打印的订单文件必须留存，
    否则重启后从数据库恢复的队列订单会因文件丢失而无法打印。
    保留 group_*：拼单等着同学陆续加入，可能跨小时，重启不能把拼单文件删掉。
    """
    if not os.path.isdir("uploads"):
        return
    for name in os.listdir("uploads"):
        if name.startswith("group_"):
            continue
        _delete_folder(os.path.join("uploads", name), name, None, "startup")
    files_attributes_singleton().data.clear()


def clean_finished_groups(keep_seconds=86400):
    """已结算（或记录已不存在）的拼单目录：24 小时后清理。

    拼单目录没有 expire 文件，不会被 clean_expired_directories 扫到；
    这里兜底清理，避免 uploads 下越堆越多的 group_* 空目录。
    """
    if not os.path.isdir("uploads"):
        return
    now = datetime.now()
    for name in os.listdir("uploads"):
        if not name.startswith("group_"):
            continue
        group_no = name[len("group_"):]
        group = db.get_group(group_no)
        if group is None:
            _delete_folder(f"./uploads/{name}", name, None, "group_missing")
            continue
        if group["status"] == "open":
            continue
        # AA 拼单锁定后仍在收款：文件必须保留，否则未付款成员无法支付
        if group["status"] == "collecting":
            continue
        # 没有结算时间时退回创建时间，绝不因为字段为空就立即删目录
        settled = _parse_time(group.get("settled_at")) or _parse_time(
            group.get("created_at"))
        if settled is None or now - settled < timedelta(seconds=keep_seconds):
            continue
        _delete_folder(f"./uploads/{name}", name, group.get("order_no"),
                       "group_done")


def clean_expired_directories():
    """上传会话 15 分钟不活跃自动删除（删除记录写 file_deletions）。"""
    if not os.path.isdir("uploads"):
        return
    now = datetime.now().timestamp()
    for directory in os.listdir("uploads"):
        # 目录里可能还没有 expire 文件（刚创建/正在上传），读不到就跳过，避免定时任务抛异常中断
        try:
            with open(f'./uploads/{directory}/expire', "r", encoding="utf-8") as f:
                expire = json.load(f).get('expire')
        except (OSError, ValueError, AttributeError):
            continue
        if expire is None or expire >= now:
            continue
        _delete_folder(f"./uploads/{directory}", directory, None, "expired")
        files_attributes_global = files_attributes_singleton()
        files_attributes_global.data.pop(directory, None)


def clean_printed_sources(keep_seconds=300):
    """打印完成后 5 分钟删除源文件。

    只清理「该会话目录下所有订单都已结束、且没有新上传在等下单」的目录，
    避免把用户刚上传的新文件一起删掉。pending_files 里的打印副本由打印机端
    上报成功时立即删除，这里只处理 uploads 下的原文件/转换文件/备份/OCR 缓存。
    """
    now = datetime.now()
    sessions = files_attributes_singleton().data
    for row in db.folder_jobs_summary():
        folder = row.get("folder")
        # 拼单目录由 clean_finished_groups 统一按拼单状态清理：
        # 这里只看到「已付款成员」的订单，AA 里未付款成员的文件会被误删
        if not folder or folder.startswith("group_"):
            continue
        total = int(row.get("total") or 0)
        finished = int(row.get("finished") or 0)
        last_completed = _parse_time(row.get("last_completed"))
        if not folder or total == 0 or finished < total or last_completed is None:
            continue
        if now - last_completed < timedelta(seconds=keep_seconds):
            continue
        if sessions.get(folder):
            # 同一会话里还有未下单的新文件，不能整目录删除
            continue
        # 新上传会先写一个新的 expire（now+15 分钟），看到未来时间说明会话又活跃了
        try:
            with open(f'./uploads/{folder}/expire', "r", encoding="utf-8") as f:
                if (json.load(f).get('expire') or 0) > now.timestamp():
                    continue
        except (OSError, ValueError, AttributeError):
            pass
        _delete_folder(f"./uploads/{folder}", folder, row.get("order_no"), "printed")
        # 重印副本与源文件同口径：订单全部完成 5 分钟后一起清理
        _delete_folder(f"./reprint_cache/{folder}", folder,
                       row.get("order_no"), "printed")


def clean_stale_pending(keep_seconds=86400):
    """失败/已完成订单残留的待打印文件：超过 24 小时清理。"""
    now = datetime.now()
    for job in db.list_jobs():
        if job.get("status") not in ("failed", "completed"):
            continue
        changed = _parse_time(job.get("status_at"))
        if changed is None or now - changed < timedelta(seconds=keep_seconds):
            continue
        folder = job.get("folder")
        filename = job.get("filename")
        path = f"pending_files/{folder}/{filename}"
        if os.path.isfile(path):
            _delete_file(path, folder, filename, job.get("out_trade_no"), "stale")
            try:
                if not os.listdir(f"pending_files/{folder}"):
                    os.rmdir(f"pending_files/{folder}")
            except OSError:
                pass


def backup_database(keep=7):
    """每日备份 printer.db 到 backups/，只保留最近 keep 份。

    用 sqlite3 的 backup API 复制，WAL 模式下也能拿到一致快照；
    失败只记日志，不影响主服务。
    """
    try:
        os.makedirs("backups", exist_ok=True)
        name = f"backups/printer-{datetime.now().strftime('%Y%m%d')}.db"
        src = sqlite3.connect(db.DB_PATH)
        try:
            dst = sqlite3.connect(name)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        files = sorted(glob.glob("backups/printer-*.db"))
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
        return name
    except Exception as exc:
        print(f"[backup] 数据库备份失败：{exc}", flush=True)
        return None


def clear_expired_bills():
    """删除过期账单。账单已落库，重启不丢；这里只做定期清理。"""
    return db.clear_expired_bills()


def release_scheduled_jobs():
    """把到点的预约订单放行进入打印队列（定时打印）。

    付款时订单已经落库并生成好待打印文件，只是没进内存队列（status='scheduled'）；
    到点后由这里改成 pending 并追加到队列，打印机端才会取到它。
    文件缺失时（例如被清理）标为 failed，不放行一张打不出来的订单。
    """
    from print_queue import print_queue_singleton
    released = []
    for job in db.list_due_scheduled_jobs():
        path = f"pending_files/{job.get('folder')}/{job.get('filename')}"
        if not os.path.isfile(path):
            db.set_job_status_by_id(job["id"], "failed")
            continue
        db.set_job_status_by_id(job["id"], "pending")
        payload = dict(job)
        payload["status"] = "pending"
        print_queue_singleton().data.append(payload)
        released.append(job["id"])
    return released


def expire_stale_scheduled():
    """预约单到点后 12 小时仍未打印时作废，避免隔天突然出纸。

    已被放行、仍停留在内存打印队列里的 pending 预约单（如设备一直离线没取走），
    作废 DB 状态的同时必须从内存队列剔除，否则设备恢复后仍会把它取走打印，
    与「作废即不再打印」的承诺矛盾。返回本次作废的订单数。
    """
    from print_queue import print_queue_singleton
    expired = db.expire_stale_scheduled(hours=12)
    if expired:
        keys = {(job.get("folder"), job.get("filename")) for job in expired}
        queue = print_queue_singleton().data
        # 原地过滤（queue[:] = ...），保持单例 list 引用不变，
        # 其它模块持有的 print_queue 引用仍然有效
        queue[:] = [job for job in queue
                    if (job.get("folder"), job.get("filename")) not in keys]
    return len(expired)
