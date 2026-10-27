"""扫描件 OCR（本地 Tesseract，不依赖云服务）。

只做扫描件的补充检查：页码、参考文献标题/条目、空白页。
字体、行距、页边距在扫描件上不可靠，明确不判定。
"""

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from io import BytesIO

import fitz

try:
    import pytesseract
    from PIL import Image
except Exception:  # pragma: no cover - 没装依赖时走降级分支
    pytesseract = None
    Image = None

PAGE_NUMBER_RE = re.compile(
    r"^(第\s*\d+\s*页(\s*共\s*\d+\s*页)?|page\s*\d+(\s*of\s*\d+)?|"
    r"\d+\s*/\s*\d+|[-—]\s*\d+\s*[-—]|\d{1,4})$", re.I)
REFERENCE_RE = re.compile(r"^\s*(\[\s*\d+\s*\]|［\s*\d+\s*］|\d+[\.、])\s*\S+")


def _tesseract_path():
    candidate = shutil.which("tesseract")
    if candidate:
        return candidate
    for path in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=1)
def _languages():
    path = _tesseract_path()
    if not path:
        return ()
    try:
        result = subprocess.run([path, "--list-langs"], capture_output=True,
                                text=True, timeout=8)
        lines = (result.stdout or "").splitlines()
        return tuple(line.strip() for line in lines
                     if line.strip() and "List of available" not in line)
    except Exception:
        return ()


def available():
    return bool(pytesseract and Image and _tesseract_path() and _preferred_lang())


def _preferred_lang():
    languages = _languages()
    if "chi_sim" in languages and "eng" in languages:
        return "chi_sim+eng"
    if "chi_sim" in languages:
        return "chi_sim"
    if "eng" in languages:
        return "eng"
    return None


def _cache_path(path):
    return path + ".ocr.json"


def _load_cache(path):
    try:
        with open(_cache_path(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("mtime") == os.path.getmtime(path):
            return data
    except Exception:
        pass
    return None


def _save_cache(path, payload):
    try:
        with open(_cache_path(path), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _ocr_page(page, lang, dpi=200):
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
    image = Image.open(BytesIO(pix.tobytes("png")))
    pytesseract.pytesseract.tesseract_cmd = _tesseract_path()
    return pytesseract.image_to_string(image, lang=lang, config="--psm 6")


def _page_number_lines(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[:2] + lines[-2:]) if lines else []


def analyze(path, template_name="general_thesis", max_pages=10):
    """对扫描件做 OCR，并给出低置信度的补充检查。"""
    if not available():
        return {
            "available": False,
            "message": "未安装 Tesseract 或中文语言包（chi_sim），扫描件只能提示上传文字版 PDF",
            "issues": [],
        }
    from doc_format import get_template  # 延迟导入，避免循环依赖

    tpl = get_template(template_name)
    lang = _preferred_lang()
    doc = fitz.open(path)
    try:
        total = doc.page_count
        limit = min(total, max_pages)
        cached = _load_cache(path)
        if cached and cached.get("lang") == lang and cached.get("limit") == limit:
            texts = cached.get("texts") or []
            from_cache = True
        else:
            texts = []
            for index in range(limit):
                page = doc.load_page(index)
                # 已有文字层的页不重复 OCR
                text = page.get_text().strip()
                texts.append(text if text else _ocr_page(page, lang))
            _save_cache(path, {
                "mtime": os.path.getmtime(path), "lang": lang,
                "limit": limit, "texts": texts,
            })
            from_cache = False

        issues = []
        missing_pn = []
        for index, text in enumerate(texts):
            if index == 0:
                continue
            if not any(PAGE_NUMBER_RE.fullmatch(line)
                       for line in _page_number_lines(text)):
                missing_pn.append(index + 1)
        if tpl.get("page_number_required") and missing_pn:
            issues.append({
                "level": "warn", "code": "page_number", "confidence": "low",
                "message": f"OCR 未在前 {limit} 页识别到页码：第 "
                           + "、".join(map(str, missing_pn[:8])) + " 页（需人工复核）",
            })

        all_text = "\n".join(texts)
        heading_found = bool(re.search(r"参考文献|references", all_text, re.I))
        entries = {line.strip()[:80] for line in all_text.splitlines()
                   if REFERENCE_RE.match(line.strip())}
        if tpl.get("references_required"):
            if not heading_found:
                issues.append({
                    "level": "warn", "code": "references", "confidence": "low",
                    "message": "OCR 未识别到「参考文献 / References」章节（需人工复核）",
                })
            elif len(entries) < int(tpl.get("reference_min_entries") or 0):
                issues.append({
                    "level": "warn", "code": "references", "confidence": "low",
                    "message": f"OCR 识别到约 {len(entries)} 条参考文献，少于模板要求（需人工复核）",
                })

        blank_pages = [index + 1 for index, text in enumerate(texts)
                       if len(text.strip()) < 3]
        if blank_pages:
            issues.append({
                "level": "info", "code": "blank", "confidence": "low",
                "message": "OCR 未识别到文字，可能是空白页或图片页：第 "
                           + "、".join(map(str, blank_pages[:8])) + " 页",
            })

        return {
            "available": True,
            "lang": lang,
            "pages_ocr": limit,
            "truncated": total > limit,
            "from_cache": from_cache,
            "issues": issues,
            "issue_count": len(issues),
            "confidence_counts": {"high": 0, "medium": 0, "low": len(issues)},
            "text_preview": (texts[0][:300] if texts else ""),
        }
    finally:
        doc.close()
