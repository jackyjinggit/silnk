import json
import logging
import os
import shutil

import fitz  # PyMuPDF：把 PDF 页面渲染成缩略图
import jose
from fastapi import Depends
from asyncio import sleep
from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, UploadFile, Header, BackgroundTasks
from jose.jwt import decode
from pydantic import BaseModel
from starlette.responses import FileResponse, Response, JSONResponse

import a3_split
import content_review
import db
import doc_format
import layout
import ocr
import pii
import pricing
import setting
from global_vars.files_attributes_singleton import files_attributes_singleton
from models import FileModel, ReturnResult, RemoveFilename, ConvertFactory
import jwt

router = APIRouter()

# 单文件大小上限：100MB。前端也会先拦一次，这里兜底防止绕过前端直接调接口。
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
# 允许上传的扩展名（与页面上的上传入口一致，后端必须独立校验）
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "jpg", "jpeg", "png"}


def _safe_filename(filename):
    """只保留文件名本身，去掉路径和反斜杠，挡住 ../ 这类路径穿越。"""
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        return None
    return name


def _safe_session_dir(directory):
    """会话目录只允许单层目录名，挡住 ../ 与绝对路径（token 已签名，这里再兜一层）。"""
    name = (directory or "").strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or name != os.path.basename(name):
        return None
    return name


def _file_ext(filename):
    """取小写扩展名；没有扩展名返回空串（调用方据此拒绝，避免 IndexError）。"""
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def _cleanup_written(paths):
    """本次请求已写入的原始文件回滚，避免留下半截脏文件。"""
    for path in paths:
        if os.path.exists(path):
            os.remove(path)



@router.put("/uploadfile")
async def create_upload_file(files: list[UploadFile],
                             background_tasks: BackgroundTasks,
                             payload: dict = Depends(jwt.verify_token)):
    directory = payload.get("token")
    # 目录可能上次上传中断只建了一半（有 uploads/{token} 但没有 raw），
    # 用 makedirs(exist_ok=True) 保证重复进入不会抛 FileExistsError。
    os.makedirs(f"uploads/{directory}/raw", exist_ok=True)
    os.makedirs(f"uploads/{directory}/converted", exist_ok=True)

    with open(f'uploads/{directory}/expire', 'w') as f:
        expire = {'expire': datetime.now().timestamp() + 60 * 15}
        json.dump(expire, f)

    written_paths = []
    for index in range(len(files)):
        safe_name = _safe_filename(files[index].filename)
        if safe_name is None:
            _cleanup_written(written_paths)
            return ReturnResult(200, "文件名不合法", {})
        filetype = _file_ext(safe_name)
        if filetype not in ALLOWED_EXTENSIONS:
            _cleanup_written(written_paths)
            return ReturnResult(
                200, f"不支持的文件类型：.{filetype}" if filetype else "文件缺少扩展名", {})

        raw_path = f'uploads/{directory}/raw/{safe_name}'
        size = 0
        try:
            # 分块落盘：100MB 文件不再整块读进内存，同时边写边校验大小
            with open(raw_path, "wb") as f:
                while True:
                    chunk = await files[index].read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("too large")
                    f.write(chunk)
        except ValueError:
            if os.path.exists(raw_path):
                os.remove(raw_path)
            _cleanup_written(written_paths)
            return ReturnResult(200, f"「{safe_name}」超过 100MB，无法上传", {})
        written_paths.append(raw_path)

        file_attributes = FileModel()
        file_attributes.filename = safe_name
        file_attributes.convert_state = "processing"  # pending
        file_attributes.print_copies = 1
        file_attributes.print_range_start = 1
        file_attributes.print_range_end = 1
        file_attributes.print_side = "one-sided"
        file_attributes.folder = directory

        files_attributes_global = files_attributes_singleton()
        files_attributes = files_attributes_global.data.get(directory) or []
        # 同名文件重传：先整体过滤掉旧记录（遍历中 remove 会漏删/抛异常）
        files_attributes = [
            fa for fa in files_attributes if fa.filename != safe_name]
        files_attributes.append(file_attributes)
        files_attributes_global.data.update({directory: files_attributes})

        convert = ConvertFactory().convert(setting.CONVERT_TOOL)
        if filetype == "pdf":
            background_tasks.add_task(convert.convert_pdf, directory, safe_name)
        elif filetype in ("doc", "docx"):
            background_tasks.add_task(convert.convert_docs, directory, safe_name)
        elif filetype in ("xlsx", "xls"):
            background_tasks.add_task(convert.convert_excel, directory, safe_name)
        elif filetype in ("jpeg", "jpg", "png"):
            background_tasks.add_task(convert.convert_images, directory, safe_name)
    return ReturnResult(200, "success", {"uploaded": len(written_paths)})


@router.get("/uploadfile/filelist")
async def get_folder(Authorization: Annotated[str | None, Header()]):
    # 头可能是 None、空串或不带空格：先拆分再取，避免 IndexError/AttributeError 变成 500
    parts = (Authorization or "").split(" ")
    if len(parts) < 2 or not parts[1]:
        return ReturnResult(200, "success", {'files_attributes': [], 'token': jwt.create_token()})
    token = parts[1]
    try:
        jose.jwt.decode(token, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception as e:
        return ReturnResult(200, "success", {'files_attributes': [], 'token': jwt.create_token()})
    renewed_token = jwt.renew_token(token)
    payload = jwt.decode_token(token)
    directory = payload.get("token")

    files_attributes_global = files_attributes_singleton()
    files_attributes = files_attributes_global.data
    if directory in files_attributes:
        with open(f'uploads/{directory}/expire', 'w') as f:
            expire = {'expire': datetime.now().timestamp() + 60 * 15}
            json.dump(expire, f)
        return ReturnResult(200, "success",{'files_attributes': files_attributes.get(directory), 'token': renewed_token})
    return ReturnResult(200, "success", {'files_attributes': [], 'token': renewed_token})

@router.get("/uploadfile/convert_status")
async def get_convert_status(payload: dict = Depends(jwt.verify_token)):
    directory = payload.get("token")
    files_attributes_global = files_attributes_singleton()
    # 目录可能尚未创建或已被清理（会话过期），此时按「无待转换文件」处理，
    # 不能直接对 None 迭代，否则接口会 500
    files_attributes = files_attributes_global.data.get(directory) or []

    converting_filenames = []
    for file_attributes in files_attributes:
        if file_attributes.convert_state == "processing":
            converting_filenames.append(file_attributes.filename)
    if len(converting_filenames) == 0:
        return ReturnResult(200, "success", {})  # if no processing files, return success

    start_unix_timestamp = datetime.now().timestamp()
    while True:
        await sleep(0.01)
        if datetime.now().timestamp() - start_unix_timestamp > 120:
            return ReturnResult(200, "timeout", {})
        files_attributes = files_attributes_global.data.get(directory) or []
        for file_attributes in files_attributes:
            for converting_filename in converting_filenames:
                if file_attributes.filename == converting_filename:
                    if file_attributes.convert_state == "success":
                        return ReturnResult(200, "success", {})
                    if file_attributes.convert_state == 'error':
                        return ReturnResult(200, "success", {})


@router.post("/uploadfile/remove")
async def remove_file(remove_filename: RemoveFilename, payload: dict = Depends(jwt.verify_token)):
    directory = payload.get("token")
    safe_name = _safe_filename(remove_filename.filename)
    if safe_name is None:
        return ReturnResult(200, "文件名不合法", {})

    files_attributes_global = files_attributes_singleton()
    files_attributes = files_attributes_global.data.get(directory)
    if not files_attributes:
        return ReturnResult(200, "success", {})
    remaining = [fa for fa in files_attributes if fa.filename != safe_name]
    files_attributes_global.data.update({directory: remaining})
    # 用户主动删除：立即删掉磁盘文件，不等 15 分钟过期，并留删除记录
    pdf_name = safe_name.rsplit(".", 1)[0] + ".pdf"
    candidates = (
        f"uploads/{directory}/raw/{safe_name}",
        f"uploads/{directory}/converted/{pdf_name}",
        f"uploads/{directory}/converted/{pdf_name[:-4]}.orig.pdf",
        f"uploads/{directory}/converted/{pdf_name}.ocr.json",
    )
    for path in candidates:
        try:
            if os.path.isfile(path):
                os.remove(path)
                db.add_file_deletion(directory, os.path.basename(path), None, "manual")
        except OSError:
            pass
    return ReturnResult(200, "success", {})


@router.get("/preview/{filename}")
def preview_pdf(filename: str, payload: dict = Depends(jwt.verify_user)):
    """预览转换后的 PDF。token 只走 Authorization header，不再放进 URL。"""
    directory = _safe_session_dir(payload.get("token"))
    safe_name = _safe_filename(filename)
    if directory is None or safe_name is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    converted_filename = safe_name.rsplit(".", 1)[0] + ".pdf"
    path = f"uploads/{directory}/converted/{converted_filename}"
    if not os.path.isfile(path):
        return JSONResponse({"message": "file not found"}, status_code=404)
    return FileResponse(path)


@router.get("/api/user/thumb/{filename}")
def page_thumbnail(filename: str, page: int = 1,
                   payload: dict = Depends(jwt.verify_user)):
    """返回指定页的 PNG 缩略图。

    前端用 fetch + Authorization 取 blob（<img> 不能带 header），
    这样会话 token 不会出现在 URL、浏览器历史与访问日志里。
    """
    directory = _safe_session_dir(payload.get("token"))
    safe_name = _safe_filename(filename)
    if directory is None or safe_name is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    pdf_name = safe_name.rsplit(".", 1)[0] + ".pdf"
    candidates = [
        f"uploads/{directory}/converted/{pdf_name}",
        f"pending_files/{directory}/{pdf_name}",
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    try:
        doc = fitz.open(path)
        if doc.page_count == 0:
            doc.close()
            return JSONResponse({"message": "empty pdf"}, status_code=404)
        index = max(0, min(int(page) - 1, doc.page_count - 1))
        pdf_page = doc.load_page(index)
        # 按宽度 220px 渲染，足够做页缩略图又不会太大
        zoom = 220.0 / max(pdf_page.rect.width, 1.0)
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        data = pix.tobytes("png")
        doc.close()
    except Exception as e:
        print(f"[thumb] render failed: {e}", flush=True)
        return JSONResponse({"message": "render failed"}, status_code=500)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "max-age=600"})


# ----------------------- 打印前检查（纯本地规则分析，不调用大模型） -----------------------
# 目的：付款前提示空白页、彩色页、内容贴边、横竖版混排，减少打错重打。
CHECK_MAX_PAGES = 200
CHECK_ZOOM = 30.0 / 72.0      # 低分辨率渲染即可判断空白与彩色，控制耗时
CHECK_MARGIN_MM = 4.0          # 多数打印机不可打印边距 4~6mm，小于 4mm 视为可能被裁切


def _find_pdf(directory, filename):
    """在会话的上传目录 / 待打印目录里找转换后的 PDF。"""
    safe = _safe_filename(filename)
    if safe is None:
        return None
    pdf_name = safe.rsplit(".", 1)[0] + ".pdf"
    for path in (f"uploads/{directory}/converted/{pdf_name}",
                 f"pending_files/{directory}/{pdf_name}"):
        if os.path.isfile(path):
            return path
    return None


def _analyze_page(page, zoom):
    """返回 (是否空白, 是否含彩色, 彩色覆盖率 0~1)。

    空白判断以「有没有文字/图片/矢量内容」为准，比像素采样可靠。
    彩色判断先看文字颜色（span.color），再用低分辨率渲染兜底识别彩色图片/色块；
    同时用同一张渲染图统计彩色像素占比，供按覆盖率分档计价使用。
    """
    has_text = bool(page.get_text().strip())
    has_images = bool(page.get_images())
    if has_text or has_images:
        blank = False
    else:
        # 只有文字和图片都没有时，才多花一次调用看矢量图形
        blank = not bool(page.get_drawings())

    text_colored = False
    # 1) 文字颜色：span.color 是 0xRRGGBB，非灰即彩色
    try:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    color = span.get("color", 0)
                    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
                    if max(r, g, b) - min(r, g, b) > 28:
                        text_colored = True
                        break
                if text_colored:
                    break
            if text_colored:
                break
    except Exception:
        pass

    coverage = 0.0
    if not blank:
        # 2) 渲染抽样：识别彩色图片/色块，并统计覆盖率
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        samples = pix.samples
        step = 3 * 4            # 每 4 个像素抽一个
        checked = 0
        colored = 0
        for i in range(0, len(samples) - 2, step):
            r, g, b = samples[i], samples[i + 1], samples[i + 2]
            checked += 1
            if max(r, g, b) - min(r, g, b) > 30 and max(r, g, b) > 60:
                colored += 1
        coverage = (colored / checked) if checked else 0.0

    colored = text_colored or coverage >= 0.001
    return blank, colored, coverage


def _page_margin_mm(page):
    """按文字块 / 图片的外接框估算最小页边距（毫米）。"""
    boxes = []
    for block in page.get_text("blocks"):
        boxes.append(fitz.Rect(block[:4]))
    for info in page.get_image_info():
        boxes.append(fitz.Rect(info["bbox"]))
    if not boxes:
        return None
    rect = boxes[0]
    for box in boxes[1:]:
        rect |= box
    left = max(rect.x0, 0)
    top = max(rect.y0, 0)
    right = max(page.rect.width - rect.x1, 0)
    bottom = max(page.rect.height - rect.y1, 0)
    return min(left, top, right, bottom) / 72 * 25.4


# 打印前体检：图片有效分辨率下限（DPI）与常见纸张尺寸（毫米）。
LOW_DPI_LIMIT = 150
PAPER_SIZES_MM = (("A4", (210.0, 297.0)), ("A3", (297.0, 420.0)),
                  ("A5", (148.0, 210.0)), ("LETTER", (215.9, 279.4)))


def _page_min_image_dpi(doc, page):
    """页面内图片的最小有效分辨率（DPI）；页内没有位图时返回 None。

    有效 DPI = 图片像素宽 / 图片在页面上的物理宽度（英寸）。
    印刷体一般要求 150 DPI 以上，低于此值放大打印会明显发虚。
    只对页内位图求值，矢量图（PDF 画的图形/文字）不受影响。
    """
    lowest = None
    try:
        images = page.get_images(full=True)
    except Exception:
        return None
    for info in images:
        try:
            pix = fitz.Pixmap(doc, info[0])
            width = pix.width
            pix = None
            box = page.get_image_bbox(info)
            if box.width <= 1 or box.height <= 1:
                continue
            dpi = width / (box.width / 72.0)
        except Exception:
            continue
        lowest = dpi if lowest is None else min(lowest, dpi)
    return lowest


def _page_size_name(page):
    """按页面物理尺寸归到 A4 / A3 / A5 / LETTER / 其他（容差 3mm）。"""
    width_mm = page.rect.width / 72.0 * 25.4
    height_mm = page.rect.height / 72.0 * 25.4
    short, long = sorted((width_mm, height_mm))
    for name, (sw, sl) in PAPER_SIZES_MM:
        if abs(short - sw) <= 3 and abs(long - sl) <= 3:
            return name
    return "其他"


@router.get("/api/user/check/{authentication}/{filename}")
def precheck_pdf(authentication: str, filename: str):
    """打印前检查：空白页 / 彩色页 / 贴边 / 横竖版混排。

    鉴权方式与缩略图一致（token 放在路径里）。
    只做规则分析，不调用任何 AI 大模型，几秒内返回；超过 100 页只检查前 100 页。
    """
    try:
        jose.jwt.decode(authentication, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception:
        return JSONResponse({"message": "fail"}, status_code=401)
    directory = jwt.decode_token(authentication).get("token")
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)

    try:
        doc = fitz.open(path)
    except Exception:
        return JSONResponse({"message": "open failed"}, status_code=500)

    total = doc.page_count
    limit = min(total, CHECK_MAX_PAGES)
    blank_pages, color_pages, tight_pages, landscape_pages = [], [], [], []
    coverage_pages = []
    low_dpi_pages = []
    size_counts = {}
    min_dpi = None
    min_margin = None
    base_landscape = None

    try:
        for index in range(limit):
            page = doc.load_page(index)
            blank, colored, coverage = _analyze_page(page, CHECK_ZOOM)
            if blank:
                blank_pages.append(index + 1)
            else:
                coverage_pages.append({
                    "page": index + 1,
                    "coverage": round(coverage * 100, 1),
                    "tier": pricing.tier_of(coverage),
                })
            if colored:
                color_pages.append(index + 1)
            landscape = page.rect.width > page.rect.height
            if base_landscape is None:
                base_landscape = landscape
            elif landscape != base_landscape:
                landscape_pages.append(index + 1)
            # 纸张规格：按页面物理尺寸归类，用于提示「选的纸和文件不符」
            size_name = _page_size_name(page)
            size_counts[size_name] = size_counts.get(size_name, 0) + 1
            if not blank:
                margin = _page_margin_mm(page)
                if margin is not None:
                    min_margin = margin if min_margin is None else min(min_margin, margin)
                    if margin < CHECK_MARGIN_MM:
                        tight_pages.append(index + 1)
                # 图片有效分辨率：低于 150 DPI 会明显发虚，提前提示
                dpi = _page_min_image_dpi(doc, page)
                if dpi is not None:
                    min_dpi = dpi if min_dpi is None else min(min_dpi, dpi)
                    if dpi < LOW_DPI_LIMIT:
                        low_dpi_pages.append(index + 1)
        try:
            a3_pages = a3_split.a3_pages_in_doc(doc)
        except Exception:
            a3_pages = []
    finally:
        doc.close()

    # 主要纸张规格 = 页数最多的那一类；混排时前端会提示「纸张不一致」
    dominant_size = None
    if size_counts:
        dominant_size = max(size_counts.items(), key=lambda kv: kv[1])[0]

    return ReturnResult(200, "success", {
        "pages": total,
        "checked_pages": limit,
        "truncated": total > limit,
        "blank_pages": blank_pages,
        "color_pages": color_pages[:10],
        "tight_pages": tight_pages[:10],
        "landscape_pages": landscape_pages[:10],
        "coverage_pages": coverage_pages[:200],
        "coverage": pricing.coverage_config(),
        "min_margin_mm": None if min_margin is None else round(min_margin, 1),
        "margin_limit_mm": CHECK_MARGIN_MM,
        "a3_pages": a3_pages,
        # 体检增强：图片分辨率、纸张规格（打印前就能发现会打糊/打错纸）
        "low_dpi_pages": low_dpi_pages[:10],
        "min_image_dpi": None if min_dpi is None else round(min_dpi),
        "dpi_limit": LOW_DPI_LIMIT,
        "size_counts": size_counts,
        "dominant_size": dominant_size,
    })


@router.get("/api/user/format/{authentication}/{filename}")
def format_check(authentication: str, filename: str, template: str = "general_thesis"):
    """格式合规检查：页边距、字体、行距、页码、封面、参考文献。"""
    try:
        jose.jwt.decode(authentication, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception:
        return JSONResponse({"message": "fail"}, status_code=401)
    directory = jwt.decode_token(authentication).get("token")
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    try:
        result = doc_format.analyze(path, template)
    except Exception:
        logging.exception("format check failed")
        return JSONResponse({"message": "格式检查失败，请重新上传后再试"}, status_code=500)
    return ReturnResult(200, "success", result)


@router.get("/api/user/ocr/{authentication}/{filename}")
def ocr_check(authentication: str, filename: str, template: str = "general_thesis"):
    """对扫描件做本地 OCR，补充页码/参考文献检查（低置信，需人工复核）。"""
    try:
        jose.jwt.decode(authentication, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception:
        return JSONResponse({"message": "fail"}, status_code=401)
    directory = jwt.decode_token(authentication).get("token")
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    try:
        result = ocr.analyze(path, template)
    except Exception:
        logging.exception("ocr failed")
        return JSONResponse({"message": "OCR 识别失败，请稍后重试"}, status_code=500)
    return ReturnResult(200, "success", result)


class FixRequest(BaseModel):
    remove_blank: bool = True
    fix_margins: bool = True
    add_page_numbers: bool = True
    insert_cover: bool = False
    duplex: bool = False
    author: str = ""
    course: str = ""
    template: str = "general_thesis"


@router.post("/api/user/fix/{authentication}/{filename}")
def fix_format(authentication: str, filename: str, request: FixRequest):
    """一键修复：删空白页、调页边距、补页码、插封面、双面奇偶配对。

    修复结果直接覆盖 converted 目录里的 PDF，原文件备份为 .orig.pdf；
    文件列表里的 total_pages 同步更新，后续计价和打印都使用修复后的文件。
    """
    try:
        jose.jwt.decode(authentication, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception:
        return JSONResponse({"message": "fail"}, status_code=401)
    directory = jwt.decode_token(authentication).get("token")
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)

    backup = path[:-4] + ".orig.pdf" if path.lower().endswith(".pdf") else path + ".orig.pdf"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".fixed"
    try:
        stats = doc_format.fix(path, tmp, {
            "remove_blank": request.remove_blank,
            "fix_margins": request.fix_margins,
            "add_page_numbers": request.add_page_numbers,
            "insert_cover": request.insert_cover,
            "duplex": request.duplex,
            "author": request.author,
            "course": request.course,
        }, template_name=request.template)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        logging.exception("fix format failed")
        return JSONResponse({"message": "修复失败，请重新上传后再试"}, status_code=500)

    # 同步内存里的页数/范围，避免前端还按旧页数计价
    for item in files_attributes_singleton().data.get(directory, []):
        if item.filename == filename:
            item.total_pages = stats["new_pages"]
            if item.print_range_start > stats["new_pages"]:
                item.print_range_start = 1
            if item.print_range_end > stats["new_pages"] or item.print_range_end < item.print_range_start:
                item.print_range_end = stats["new_pages"]
            break
    stats["total_pages"] = stats["new_pages"]
    return ReturnResult(200, "success", stats)


class SplitA3Request(BaseModel):
    direction: str = "auto"


@router.post("/api/user/split-a3/{authentication}/{filename}")
def split_a3(authentication: str, filename: str, request: SplitA3Request):
    """A3 拆 A4：把 A3 页面按方向切成两页 A4，A4 页面原样保留。

    direction=auto 时按纸张方向自动判断：横向 A3 左右切、纵向 A3 上下切。
    拆分结果覆盖原 PDF，原文件备份为 .orig.pdf。
    """
    try:
        jose.jwt.decode(authentication, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception:
        return JSONResponse({"message": "fail"}, status_code=401)
    directory = jwt.decode_token(authentication).get("token")
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)

    tmp = path + ".split"
    try:
        stats = a3_split.split(path, tmp, request.direction)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        logging.exception("split a3 failed")
        return JSONResponse({"message": "A3 拆分失败，请重新上传后再试"}, status_code=500)

    if not stats["a3_pages"]:
        os.remove(tmp)
        stats["total_pages"] = stats["original_pages"]
        return ReturnResult(200, "未检测到 A3 页面", stats)

    backup = path[:-4] + ".orig.pdf" if path.lower().endswith(".pdf") else path + ".orig.pdf"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    os.replace(tmp, path)

    # 同步内存里的页数/范围，避免前端还按旧页数计价
    for item in files_attributes_singleton().data.get(directory, []):
        if item.filename == filename:
            item.total_pages = stats["new_pages"]
            if item.print_range_start > stats["new_pages"]:
                item.print_range_start = 1
            if item.print_range_end > stats["new_pages"] or item.print_range_end < item.print_range_start:
                item.print_range_end = stats["new_pages"]
            break
    stats["total_pages"] = stats["new_pages"]
    return ReturnResult(200, "success", stats)


class LayoutRequest(BaseModel):
    """排版：per_sheet=1 恢复原样，2/4/6 为每张纸合并的页数。"""

    per_sheet: int = 1


def _sync_page_count(directory, filename, pages, per_sheet=None):
    """排版后同步内存里的总页数与打印范围，避免前端仍按旧页数计价。"""
    for item in files_attributes_singleton().data.get(directory, []):
        if item.filename != filename:
            continue
        item.total_pages = pages
        if per_sheet is not None:
            item.layout_per_sheet = per_sheet
        if item.print_range_start > pages:
            item.print_range_start = 1
        if item.print_range_end > pages or item.print_range_end < item.print_range_start:
            item.print_range_end = pages
        break


@router.post("/api/user/layout/{authentication}/{filename}")
def apply_layout(authentication: str, filename: str, request: LayoutRequest):
    """多页合一排版：2/4/6 页合并到一张 A4，覆盖原 PDF。

    第一次排版时保留原始 PDF 备份（.layout-src.pdf），之后切换档位都从备份重新生成，
    因此可以反复切换而不会越缩越小；per_sheet=1 恢复原始页数。
    """
    try:
        jose.jwt.decode(authentication, setting.SECRET_KEY, algorithms=[setting.ALGORITHM])
    except Exception:
        return JSONResponse({"message": "fail"}, status_code=401)
    directory = jwt.decode_token(authentication).get("token")
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)

    per_sheet = int(request.per_sheet or 1)
    if per_sheet not in (1, 2, 4, 6):
        return ReturnResult(200, "只支持 1 / 2 / 4 / 6 页每张", {})

    backup = (path[:-4] + ".layout-src.pdf" if path.lower().endswith(".pdf")
              else path + ".layout-src.pdf")
    tmp = path + ".layout"
    try:
        if per_sheet == 1:
            if not os.path.exists(backup):
                pages = fitz.open(path).page_count
                return ReturnResult(200, "当前已是原始排版", {
                    "per_sheet": 1, "original_pages": pages,
                    "new_pages": pages, "total_pages": pages,
                })
            shutil.copy2(backup, path)
            pages = fitz.open(path).page_count
            stats = {"per_sheet": 1, "original_pages": pages,
                     "new_pages": pages, "total_pages": pages}
        else:
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            stats = layout.nup(backup, tmp, per_sheet)
            if not os.path.isfile(tmp):
                return ReturnResult(200, "排版失败，请重试", {})
            os.replace(tmp, path)
            stats["total_pages"] = stats["new_pages"]
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        logging.exception("layout failed")
        return JSONResponse({"message": "排版失败，请重新上传后再试"}, status_code=500)

    _sync_page_count(directory, filename, stats["new_pages"], per_sheet)
    return ReturnResult(200, "success", stats)


@router.post("/api/user/review/{filename}")
def content_review_api(filename: str, payload: dict = Depends(jwt.verify_user)):
    """印前内容审核：渲染前 N 页交给 GLM 视觉模型，结果落库。

    10 分钟内同一文件的报告直接复用，避免重复调用产生费用。
    未配置 GLM_API_KEY 时如实返回「未接入」，不假装审核通过。
    """
    directory = _safe_session_dir(payload.get("token"))
    safe_name = _safe_filename(filename)
    if directory is None or safe_name is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)

    cached = db.get_compliance_report(filename)
    if cached:
        try:
            created = datetime.strptime(cached["created_at"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - created).total_seconds() < 600:
                return ReturnResult(200, "success", cached)
        except (TypeError, ValueError):
            pass

    try:
        result = content_review.review(path)
    except Exception:
        logging.exception("content review failed")
        return JSONResponse({"message": "内容审核失败，请重试"}, status_code=500)
    if not result.get("available"):
        return ReturnResult(200, "未接入", result)
    db.add_compliance_report(filename, result)
    return ReturnResult(200, "success", result)


@router.get("/api/user/pii/{filename}")
def pii_scan(filename: str, payload: dict = Depends(jwt.verify_user)):
    """打印前隐私检查：识别文本层里的身份证号、手机号、银行卡号与住址。

    只做本地正则匹配；扫描件没有文字层时 scanned=True，前端据此提示人工确认。
    """
    directory = _safe_session_dir(payload.get("token"))
    safe_name = _safe_filename(filename)
    if directory is None or safe_name is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    try:
        result = pii.scan(path)
    except Exception:
        logging.exception("pii scan failed")
        return JSONResponse({"message": "隐私检查失败，请重试"}, status_code=500)
    return ReturnResult(200, "success", result)


@router.post("/api/user/pii-redact/{filename}")
def pii_redact(filename: str, payload: dict = Depends(jwt.verify_user)):
    """一键打码：把身份证号/手机号/银行卡号/住址覆盖为黑块，覆盖原 PDF，原文件备份 .orig.pdf。"""
    directory = _safe_session_dir(payload.get("token"))
    safe_name = _safe_filename(filename)
    if directory is None or safe_name is None:
        return JSONResponse({"message": "file not found"}, status_code=404)
    path = _find_pdf(directory, filename)
    if path is None:
        return JSONResponse({"message": "file not found"}, status_code=404)

    backup = path[:-4] + ".orig.pdf" if path.lower().endswith(".pdf") else path + ".orig.pdf"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".redact"
    try:
        result = pii.redact(path, tmp)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        logging.exception("pii redact failed")
        return JSONResponse({"message": "打码失败，请重试"}, status_code=500)
    if not result["redacted"]:
        return ReturnResult(200, "no match", result)
    return ReturnResult(200, "success", result)


@router.get("/api/user/order/{order_id}/eject")
def user_order_eject_events(order_id: int, payload: dict = Depends(jwt.verify_token)):
    job = db.get_job_by_id(order_id)
    uid = payload.get("uid")
    owned = (job is not None and
             ((uid and job.get("user_id") == int(uid))
              or (not job.get("user_id") and job.get("folder") == payload.get("token"))))
    if not owned:
        return JSONResponse({"message": "订单不存在"}, status_code=404)
    events = db.list_eject_events_for_order(order_id)
    for event in events:
        event["photo_url"] = (f"/api/user/order/{order_id}/eject/"
                              f"{event['event_id']}/photo" if event["has_photo"] else None)
    return {"message": "success", "data": {"events": events}}


@router.get("/api/user/order/{order_id}/eject/{event_id}/photo")
def user_order_eject_photo(order_id: int, event_id: str,
                           payload: dict = Depends(jwt.verify_token)):
    job = db.get_job_by_id(order_id)
    uid = payload.get("uid")
    owned = (job is not None and
             ((uid and job.get("user_id") == int(uid))
              or (not job.get("user_id") and job.get("folder") == payload.get("token"))))
    if not owned:
        return JSONResponse({"message": "订单不存在"}, status_code=404)
    event = next((item for item in db.list_eject_events_for_order(order_id)
                  if item["event_id"] == event_id and item["has_photo"]), None)
    if event is None:
        return JSONResponse({"message": "photo not found"}, status_code=404)
    image, mime = db.get_eject_photo(job.get("printer_id"), event_id)
    if image is None:
        return JSONResponse({"message": "photo not found"}, status_code=404)
    return Response(content=image, media_type=mime or "image/jpeg",
                    headers={"Cache-Control": "private, no-store"})


@router.get("/api/user/orders")
def user_orders(payload: dict = Depends(jwt.verify_token)):
    """当前登录账号的订单列表（跨设备历史）。

    登录后按 user_id 查询，换设备也能看到自己的订单；
    早期匿名订单没有 user_id，不再自动归并。
    """
    directory = payload.get("token")
    uid = payload.get("uid")
    jobs = (db.list_jobs_by_user(int(uid)) if uid
            else db.list_jobs_by_folder(directory))
    payments = {p.get("out_trade_no"): p for p in db.list_payments()}
    for job in jobs:
        start = job.get("print_range_start") or 1
        end = job.get("print_range_end") or start
        pages = max(end - start + 1, 0)
        copies = job.get("print_copies") or 1
        sheets = (pages if job.get("print_side") == "one-sided"
                  else (pages + 1) // 2) * copies
        payment = payments.get(job.get("out_trade_no")) or {}
        # 打印机端文件名是订单号；用户端展示真实文件名
        job["filename"] = job.get("display_name") or job.get("filename")
        if job.get("batch_pages") and job.get("batch_copies"):
            # 批量包：文件里已经拼了「姓名封皮 + N 份正文」，
            # 对用户展示「每份正文页数 × 份数」，而不是整包总页数。
            job["pages"] = job["batch_pages"]
            job["print_copies"] = job["batch_copies"]
            job["batch_bundle"] = True
        else:
            job["pages"] = pages
        job["sheets"] = sheets
        job["payment_status"] = payment.get("status")
    return {"message": "success", "data": {"orders": jobs}}


class ReprintRequest(BaseModel):
    """免费重印申请：格式不对 / 打印质量问题，审核通过后免费重印。"""

    reason: str = ""


@router.post("/api/user/reprint/{order_id}")
def apply_reprint(order_id: int, request: ReprintRequest,
                  payload: dict = Depends(jwt.verify_token)):
    """申请免费重印。

    鉴权与归属：登录账号必须与订单 user_id 一致（老订单回落到会话目录）。
    只允许已完成订单申请；已申请中 / 已通过的不能重复申请。
    """
    job = db.get_job_by_id(order_id)
    uid = payload.get("uid")
    owned = (job is not None and
             ((uid and job.get("user_id") == int(uid))
              or (not job.get("user_id") and job.get("folder") == payload.get("token"))))
    if not owned:
        return ReturnResult(200, "订单不存在", {})
    if job.get("status") != "completed":
        return ReturnResult(200, "只有打印完成的订单可以申请重印", {})
    if job.get("reprint_status") in ("requested", "approved"):
        return ReturnResult(200, "重印申请已提交，请等待处理", {})
    ok = db.request_reprint(order_id, request.reason)
    if not ok:
        return ReturnResult(200, "申请失败，请稍后再试", {})
    return ReturnResult(200, "success",
                        {"order_id": order_id, "reprint_status": "requested"})


@router.post("/api/user/order/{order_id}/cancel")
def cancel_scheduled_order(order_id: int, payload: dict = Depends(jwt.verify_token)):
    """取消一张还没到点的预约单（定时打印）。

    只允许取消 status='scheduled'（尚未放行到打印队列）的订单：
    已经开打、已完成或已作废的订单不能撤，如实告知原因。
    退款口径：个人收款码没有自动退款通道，这里只把收款流水标为
    「免收 / 待退款」，由线下人工处理，不假装已退款。
    """
    uid = payload.get("uid")
    # 同时传会话 token（对匿名会话即订单目录）：登录用户按 uid 校验归属，
    # 匿名会话按目录校验，避免无 uid 时跳过归属检查被越权取消。
    ok, message, job = db.cancel_scheduled_job(
        order_id, int(uid) if uid else None, payload.get("token"))
    if not ok:
        return ReturnResult(200, message, {})
    out_trade_no = job.get("out_trade_no")
    # 已经提交过收款流水的：标为「免收 / 待人工退款」，绝不假装自动退款
    new_status = db.waive_payment(out_trade_no) if out_trade_no else None
    note = ("预约已取消，不会再打印。"
            + ("该单收款状态已记为「待退款」，需人工退回。" if new_status
               in ("waived", "refund_pending") else "若已付款，请联系现场工作人员退回。"))
    return ReturnResult(200, "success", {
        "order_id": order_id,
        "status": "canceled",
        "payment_status": new_status,
        "note": note,
    })
