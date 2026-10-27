"""共享资料库接口：公开浏览、用户取用、商家复审。"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db
import jwt
import library
from convert.convert_libreoffice import soffice_bin
from global_vars.files_attributes_singleton import files_attributes_singleton
from models import FileModel, ReturnResult

router = APIRouter()

ALLOWED_UPLOAD_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class RejectBody(BaseModel):
    reason: str = ""


class CategoryBody(BaseModel):
    name: str


def _public_item(doc):
    """公开列表只暴露必要字段，不带服务端路径与上传者信息。"""
    return {
        "id": doc["id"],
        "title": doc["title"],
        "category": doc["category"],
        "pages": doc["pages"],
        "size_bytes": doc["size_bytes"],
        "downloads": doc["downloads"],
        "print_count": doc["print_count"],
        "print_pages": doc["print_pages"],
        "published_at": doc["published_at"],
    }


# ----------------------- 商家复审（必须在动态路由之前声明） -----------------------

@router.get("/api/library/admin/list")
def admin_list(status: str = "pending", _role=Depends(jwt.verify_merchant_key)):
    """待审 / 已公开 / 已驳回列表；status=pending 时包含 agent 初审中。"""
    if status == "pending":
        docs = db.list_library_docs(status=("pending_agent", "pending_admin"),
                                    limit=100)
    elif status == "published":
        docs = db.list_library_docs(status="published", limit=100)
    elif status == "rejected":
        docs = db.list_library_docs(status="rejected", limit=100)
    else:
        docs = db.list_library_docs(limit=100)
    return ReturnResult(200, "success", {
        "docs": docs,
        "counts": db.library_status_counts(),
        "discount": library.DISCOUNT_TEXT,
    })


@router.post("/api/library/admin/{doc_id}/approve")
def admin_approve(doc_id: int, _role=Depends(jwt.verify_merchant_key)):
    ok, message = library.publish(doc_id, reviewer="商家管理员")
    return ReturnResult(200, message, {"id": doc_id, "ok": ok})


@router.post("/api/library/admin/{doc_id}/reject")
def admin_reject(doc_id: int, body: RejectBody,
                 _role=Depends(jwt.verify_merchant_key)):
    ok, message = library.reject(doc_id, body.reason, reviewer="商家管理员")
    return ReturnResult(200, message, {"id": doc_id})


@router.get("/api/library/admin/{doc_id}/preview")
def admin_preview(doc_id: int, _role=Depends(jwt.verify_merchant_key)):
    """管理员预览待审文件（带鉴权，返回 PDF 流）。"""
    doc = db.get_library_doc(doc_id)
    if not doc:
        return ReturnResult(200, "资料不存在", {})
    path = doc.get("pending_path") or doc.get("published_path") or ""
    real = os.path.realpath(path)
    allowed = (os.path.realpath(library.PENDING_DIR),
               os.path.realpath(library.PUBLIC_DIR))
    if not path or not os.path.isfile(real) or not any(
            real.startswith(root + os.sep) for root in allowed):
        return ReturnResult(200, "文件不存在", {})
    return FileResponse(real, media_type="application/pdf",
                        filename=f"{doc['title']}.pdf")


@router.get("/api/library/admin/categories")
def admin_categories(_role=Depends(jwt.verify_merchant_key)):
    """分类列表（含每个分类下的资料数量）。"""
    cats = db.list_library_categories()
    for cat in cats:
        cat["count"] = db.count_library_docs_by_category(cat["name"])
    return ReturnResult(200, "success", {"categories": cats})


@router.post("/api/library/admin/categories")
def admin_add_category(body: CategoryBody,
                       _role=Depends(jwt.verify_merchant_key)):
    ok, message = library.add_category(body.name)
    return ReturnResult(200, message, {"ok": ok})


@router.delete("/api/library/admin/categories/{name}")
def admin_delete_category(name: str,
                          _role=Depends(jwt.verify_merchant_key)):
    ok, message = library.delete_category(name)
    return ReturnResult(200, message, {"ok": ok})


@router.post("/api/library/admin/upload")
async def admin_upload(file: UploadFile = File(...),
                       title: str = Form(""),
                       category: str = Form("其他"),
                       _role=Depends(jwt.verify_merchant_key)):
    """管理员上传资料：PDF 直接用，Word/Excel 由 LibreOffice 转 PDF。

    管理员上传视为已审核，直接公开；不做 agent 初审（管理员本身就是复审人）。
    """
    raw_name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
    ext = os.path.splitext(raw_name)[1].lower()
    if not raw_name or ext not in ALLOWED_UPLOAD_EXT:
        return ReturnResult(200, "只支持 PDF、Word、Excel 文件", {})

    library.ensure_dirs()
    tmpdir = tempfile.mkdtemp(prefix="libup-", dir=library.PENDING_DIR)
    local_path = os.path.join(tmpdir, "upload" + ext)
    size = 0
    try:
        with open(local_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError("too large")
                out.write(chunk)
        if size == 0:
            return ReturnResult(200, "文件为空", {})

        pdf_path = local_path
        if ext != ".pdf":
            soffice = soffice_bin()
            if not soffice:
                return ReturnResult(200, "服务器未安装 LibreOffice，暂不能转换 Word/Excel", {})
            subprocess.run(
                [soffice, "--headless", "--norestore",
                 f"-env:UserInstallation=file:///tmp/lo_lib_{os.getpid()}",
                 "--convert-to", "pdf", "--outdir", tmpdir, local_path],
                check=True, timeout=120,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pdf_path = os.path.join(
                tmpdir, os.path.splitext(os.path.basename(local_path))[0] + ".pdf")
            if not os.path.isfile(pdf_path):
                return ReturnResult(200, "文档转换失败，请确认文件没有损坏", {})

        doc_id, message = library.publish_external(
            pdf_path, title.strip() or raw_name, category,
            uploader_name="管理员上传")
        if not doc_id:
            return ReturnResult(200, message, {})
        doc = db.get_library_doc(doc_id)
        return ReturnResult(200, "success", {
            "id": doc_id,
            "title": doc["title"],
            "category": doc["category"],
            "pages": doc["pages"],
        })
    except ValueError:
        return ReturnResult(200, "文件超过 100MB，请压缩后再上传", {})
    except subprocess.TimeoutExpired:
        return ReturnResult(200, "文档转换超时，请稍后重试", {})
    except Exception as exc:
        return ReturnResult(200, f"上传失败：{exc}", {})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ----------------------- 公开浏览 -----------------------

@router.get("/api/library/categories")
def public_categories():
    """公开分类列表（用户端下单页 / 资料库筛选用）。"""
    return ReturnResult(200, "success",
                        {"categories": library.list_categories()})


@router.get("/api/library/list")
def public_list(q: str = "", category: str = ""):
    docs = db.list_library_docs(status="published", q=q, limit=100)
    if category:
        docs = [d for d in docs if d["category"] == category]
    return ReturnResult(200, "success", {
        "docs": [_public_item(d) for d in docs],
        "total": db.count_library_docs(status="published"),
        "categories": library.list_categories(),
        "discount": library.DISCOUNT_TEXT,
    })


@router.get("/api/library/my-stats")
def my_stats(payload: dict = Depends(jwt.verify_token)):
    """当前用户的资料库贡献统计：打印量、贡献折扣、下一档还差多少。"""
    return ReturnResult(200, "success",
                        library.contribution_of(payload.get("uid")))


@router.get("/api/library/{doc_id}/file")
def public_file(doc_id: int):
    doc = db.get_library_doc(doc_id)
    if not doc or doc["status"] != "published":
        return ReturnResult(200, "资料不存在或未公开", {})
    path = doc.get("published_path") or ""
    if not library.is_safe_public_path(path):
        return ReturnResult(200, "文件不存在", {})
    db.bump_library_download(doc_id)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{doc['title']}.pdf")


# ----------------------- 用户取用：复制到本次会话 -----------------------

def _unique_pdf_name(folder, title):
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(title or "资料库文档")).strip()
    base = (base or "资料库文档")[:40]
    candidate = base + ".pdf"
    existing = {f.filename for f in (files_attributes_singleton().data.get(folder) or [])}
    index = 2
    while (candidate in existing
           or os.path.exists(f"uploads/{folder}/converted/{candidate}")):
        candidate = f"{base}({index}).pdf"
        index += 1
    return candidate


@router.post("/api/library/{doc_id}/use")
def use_library_doc(doc_id: int, payload: dict = Depends(jwt.verify_token)):
    """把公开资料复制到当前上传会话，回到首页即可直接下单打印。"""
    doc = db.get_library_doc(doc_id)
    if not doc or doc["status"] != "published":
        return ReturnResult(200, "资料不存在或未公开", {})
    path = doc.get("published_path") or ""
    if not library.is_safe_public_path(path):
        return ReturnResult(200, "文件不存在", {})

    folder = payload.get("token")
    if not folder:
        return ReturnResult(200, "会话无效，请刷新页面后重试", {})
    converted_dir = f"uploads/{folder}/converted"
    os.makedirs(converted_dir, exist_ok=True)
    filename = _unique_pdf_name(folder, doc["title"])
    shutil.copy2(path, os.path.join(converted_dir, filename))

    # 续期会话，避免刚取用就因 15 分钟不活跃被清理
    try:
        os.makedirs(f"uploads/{folder}", exist_ok=True)
        with open(f"uploads/{folder}/expire", "w", encoding="utf-8") as f:
            json.dump({"expire": time.time() + 60 * 15}, f)
    except OSError:
        pass

    pages = doc["pages"] or 1
    file_attributes = FileModel()
    file_attributes.filename = filename
    file_attributes.convert_state = "success"
    file_attributes.total_pages = pages
    file_attributes.print_copies = 1
    file_attributes.print_range_start = 1
    file_attributes.print_range_end = pages
    file_attributes.print_side = "one-sided"
    file_attributes.folder = folder
    file_attributes.paper = "A4"
    file_attributes.print_color = "bw"
    file_attributes.skip_blank = True
    file_attributes.format_template = "free"
    # 记录资料 id：下单付费后据此累计「真实打印量」
    file_attributes.library_doc_id = int(doc_id)

    singleton = files_attributes_singleton()
    current = singleton.data.get(folder) or []
    current = [f for f in current if f.filename != filename]
    current.append(file_attributes)
    singleton.data.update({folder: current})

    return ReturnResult(200, "success", {
        "filename": filename,
        "pages": pages,
        "note": "已加入本次上传列表，可以继续选规格并下单。",
    })
