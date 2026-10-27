"""共享资料库：用户自愿共享 → agent 初审 → 商家管理员复审 → 公开。

如实说明：
- agent 初审复用 content_review（GLM 视觉审核）；未配置 Key 时记为 unavailable，
  直接转人工复审，绝不假装「AI 已通过」。
- 本地先用 pii.scan 检查身份证号/手机号/银行卡号/住址；命中时标记为 warn，提示管理员谨慎公开。
- 只有管理员复审通过的文件才会移动到 static/library/files 并通过接口对外提供。
"""

import os
import re
import shutil
import threading
import time

import fitz

import content_review
import db
import pii

# 共享资料库优惠：当前订单勾选共享时立减 5%（前后端共用这一常量）
DISCOUNT_RATE = 0.05
DISCOUNT_TEXT = "95 折"

# 贡献折扣：按该用户已公开资料被「真实打印」的累计次数分档。
# 打印量越多，说明资料越有价值，后续下单折扣越高（与共享 5% 取较高值，不叠加）。
CONTRIBUTION_TIERS = ((50, 0.10), (20, 0.08), (5, 0.05), (1, 0.03))

# 首次建库时预置的默认分类；之后分类由管理端维护，不再写死。
DEFAULT_CATEGORIES = ("论文", "课件", "习题", "文学", "法规", "通识", "简历", "其他")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_DIR = os.path.join(BASE_DIR, "library_pending")
PUBLIC_DIR = os.path.join(BASE_DIR, "static", "library", "files")


def ensure_dirs():
    os.makedirs(PENDING_DIR, exist_ok=True)
    os.makedirs(PUBLIC_DIR, exist_ok=True)


def clean_title(raw):
    """只保留文件名本身，去掉路径与控制字符，限制长度。"""
    name = os.path.basename(str(raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return (name or "未命名资料")[:60]


def list_categories():
    """当前可用分类（来自数据库，管理端可增删）。"""
    try:
        rows = db.list_library_categories()
        if rows:
            return [row["name"] for row in rows]
    except Exception:
        pass
    return list(DEFAULT_CATEGORIES)


def add_category(raw):
    """新增分类，返回 (是否成功, 提示)。"""
    name = str(raw or "").strip()
    if not name:
        return False, "分类名称不能为空"
    if len(name) > 20:
        return False, "分类名称最多 20 个字"
    if db.category_exists(name):
        return False, "分类已存在"
    return ((True, "success") if db.add_library_category(name)
            else (False, "新增失败"))


def delete_category(raw):
    """删除分类；「其他」与仍被资料使用的分类不允许删除。"""
    name = str(raw or "").strip()
    if name == "其他":
        return False, "「其他」是默认分类，不能删除"
    if not db.category_exists(name):
        return False, "分类不存在"
    used = db.count_library_docs_by_category(name)
    if used:
        return False, f"该分类下还有 {used} 份资料，不能删除"
    return ((True, "success") if db.delete_library_category(name)
            else (False, "删除失败"))


def clean_category(raw):
    """校验分类是否存在；不存在时归入「其他」。"""
    value = str(raw or "").strip()[:20]
    if not value:
        return "其他"
    try:
        if db.category_exists(value):
            return value
    except Exception:
        pass
    return "其他"


def contribution_of(uid):
    """用户已公开资料的打印量统计 + 贡献折扣档位。

    打印量只统计「已公开资料被付费打印」的次数，下载和取用不算。
    """
    stats = {"docs": 0, "print_count": 0, "print_pages": 0}
    if uid:
        try:
            stats = db.library_contributor_stats(int(uid))
        except Exception:
            stats = {"docs": 0, "print_count": 0, "print_pages": 0}

    rate = 0.0
    for need, tier_rate in CONTRIBUTION_TIERS:
        if stats["print_count"] >= need:
            rate = tier_rate
            break

    next_need = None
    next_rate = None
    for need, tier_rate in sorted(CONTRIBUTION_TIERS):
        if stats["print_count"] < need:
            next_need, next_rate = need, tier_rate
            break

    return {
        "docs": stats["docs"],
        "print_count": stats["print_count"],
        "print_pages": stats["print_pages"],
        "contribution_rate": rate,
        "next_need": next_need,
        "next_rate": next_rate,
        "share_rate": DISCOUNT_RATE,
    }


def _mask_phone(phone):
    phone = str(phone or "")
    if len(phone) == 11:
        return phone[:3] + "****" + phone[-4:]
    return phone


def _uploader_name(uid):
    if not uid:
        return "匿名用户"
    user = db.get_user(int(uid))
    if not user:
        return f"用户{uid}"
    return user.get("nickname") or _mask_phone(user.get("phone")) or f"用户{uid}"


def capture_pending(pending_pdf, title, category, uploader_uid=None,
                    tenant_id="default", order_no="", index=0):
    """把本次打印的成品 PDF 复制到待审目录并落库，随后异步跑 agent 初审。

    只复制「实际要打印的页范围」成品，不复制用户原始文件，避免额外泄露。
    """
    if not pending_pdf or not os.path.isfile(pending_pdf):
        return None
    ensure_dirs()
    safe_order = re.sub(r"[^A-Za-z0-9_-]", "_", str(order_no or "ORDER"))[:48]
    dest = os.path.join(PENDING_DIR, f"{safe_order}-{int(index)}.pdf")
    shutil.copy2(pending_pdf, dest)

    pages = 0
    try:
        with fitz.open(dest) as doc:
            pages = doc.page_count
    except Exception:
        pages = 0
    size_bytes = os.path.getsize(dest)

    doc_id = db.add_library_doc(
        title=clean_title(title),
        category=clean_category(category),
        pages=pages,
        size_bytes=size_bytes,
        pending_path=dest,
        uploader_uid=uploader_uid,
        uploader_name=_uploader_name(uploader_uid),
        tenant_id=tenant_id or "default",
        order_no=str(order_no or ""),
    )
    threading.Thread(target=_run_agent_review, args=(doc_id,), daemon=True).start()
    return doc_id


def _run_agent_review(doc_id):
    """agent 初审：本地 PII 扫描 + GLM 视觉审核，结果如实落库。"""
    doc = db.get_library_doc(doc_id)
    if not doc:
        return
    path = doc.get("pending_path") or ""
    if not path or not os.path.isfile(path):
        db.set_library_agent_result(doc_id, "unknown", "待审文件不存在", "")
        return

    pii_total = 0
    try:
        pii_total = int(pii.scan(path).get("total") or 0)
    except Exception:
        pii_total = 0

    try:
        result = content_review.review(path)
    except Exception as exc:  # 审核调用异常也要如实记录
        result = {"available": False, "message": str(exc)}

    pii_note = (f"本地检测到 {pii_total} 处敏感信息"
                "（身份证号 / 手机号 / 银行卡号 / 住址），需管理员确认"
                if pii_total else "")

    if not result.get("available"):
        reason = "AI 初审未接入：" + (result.get("message") or "缺少配置")
        if pii_note:
            reason += "；" + pii_note
        db.set_library_agent_result(doc_id, "unavailable", reason, "")
        return

    verdict = str(result.get("verdict") or "unknown")
    reason = str(result.get("reason") or "")
    if verdict != "block" and pii_note:
        verdict = "warn"
        reason = pii_note + ("；" + reason if reason else "")
    db.set_library_agent_result(doc_id, verdict, reason,
                                result.get("model") or "")


def publish_external(pdf_path, title, category, uploader_name="管理员上传"):
    """管理员后台上传：视为已审核，直接发布到资料库。

    只接受已经转好的 PDF；返回 (doc_id, message)，失败时 doc_id 为 None。
    """
    if not pdf_path or not os.path.isfile(pdf_path):
        return None, "文件不存在"
    if os.path.getsize(pdf_path) <= 0:
        return None, "文件为空"
    ensure_dirs()
    dest = os.path.join(
        PENDING_DIR,
        f"admin-{int(time.time() * 1000)}-{os.getpid()}.pdf")
    try:
        shutil.copy2(pdf_path, dest)
        with fitz.open(dest) as doc:
            pages = doc.page_count
    except Exception as exc:
        if os.path.isfile(dest):
            os.remove(dest)
        return None, f"PDF 无法读取：{exc}"

    doc_id = db.add_library_doc(
        clean_title(title), clean_category(category), pages,
        os.path.getsize(dest), dest, uploader_uid=None,
        uploader_name=uploader_name, order_no="ADMIN_UPLOAD")
    db.set_library_agent_result(
        doc_id, "pass", "管理员上传，视为已审核", "manual")
    ok, message = publish(doc_id, reviewer=uploader_name)
    if not ok:
        return None, message
    return doc_id, "success"


def publish(doc_id, reviewer=""):
    """管理员复审通过：把待审文件移到公开目录并标记 published。"""
    doc = db.get_library_doc(doc_id)
    if not doc:
        return False, "资料不存在"
    if doc["status"] != "pending_admin":
        return False, "当前状态不能通过（需要待管理员复审）"
    src = doc.get("pending_path") or ""
    if not src or not os.path.isfile(src):
        return False, "待审文件已丢失，无法通过"
    ensure_dirs()
    filename = f"library-{int(doc_id)}.pdf"
    dest = os.path.join(PUBLIC_DIR, filename)
    try:
        shutil.move(src, dest)
    except Exception as exc:
        return False, f"文件移动失败：{exc}"
    db.set_library_published(doc_id, filename, dest, reviewer=reviewer)
    return True, "success"


def reject(doc_id, reason, reviewer=""):
    """管理员驳回：删除待审文件并记录原因。"""
    doc = db.get_library_doc(doc_id)
    if not doc:
        return False, "资料不存在"
    if doc["status"] == "published":
        return False, "已公开的资料不能驳回"
    path = doc.get("pending_path") or ""
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    db.set_library_rejected(doc_id, str(reason or "未通过审核")[:200],
                            reviewer=reviewer)
    return True, "success"


def cleanup_pending(days=7):
    """自动关闭长期未审完的资料，删除待审文件，避免占用磁盘。"""
    removed = 0
    for doc in db.list_stale_library_pending(days=days):
        path = doc.get("pending_path") or ""
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        db.set_library_rejected(doc["id"], f"超过 {days} 天未完成审核，自动关闭")
        removed += 1
    return removed


def is_safe_public_path(path):
    """只允许读取公开目录内的文件，挡住路径穿越。"""
    real = os.path.realpath(path or "")
    root = os.path.realpath(PUBLIC_DIR)
    return real.startswith(root + os.sep) and os.path.isfile(real)
