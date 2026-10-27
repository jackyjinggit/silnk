"""论文/作业格式检查与一键修复。

设计原则：可测量的用规则，语义项只做提示；扫描件明确降级，不假装能识别。
模板放在 templates/*.json，新增学校模板不需要改代码。
"""

import json
import os
import re

import fitz

MM = 72.0 / 25.4  # 1mm = 2.8346pt

PAGE_NUMBER_RE = re.compile(
    r"^(第\s*\d+\s*页(\s*共\s*\d+\s*页)?|page\s*\d+(\s*of\s*\d+)?|"
    r"\d+\s*/\s*\d+|[-—]\s*\d+\s*[-—]|\d{1,4})$", re.I)

BUILTIN_TEMPLATES = {
    "general_thesis": {
        "id": "general_thesis",
        "name": "通用论文模板",
        "margins_mm": {"top": 25, "bottom": 25, "left": 30, "right": 20},
        "font_whitelist": [
            "simsun", "songti", "stsong", "nsimsun", "宋体",
            "simhei", "heiti", "stheiti", "黑体",
            "times", "timesnewroman", "arial", "calibri", "cambria",
            "microsoftyahei", "yahei", "微软雅黑",
        ],
        "line_spacing": 1.5,
        "page_number_required": True,
        "cover_required": True,
        "references_required": True,
        "reference_min_entries": 3,
    },
    "general_homework": {
        "id": "general_homework",
        "name": "通用作业模板",
        "margins_mm": {"top": 20, "bottom": 20, "left": 20, "right": 20},
        "font_whitelist": [
            "simsun", "songti", "stsong", "nsimsun", "宋体",
            "simhei", "heiti", "stheiti", "黑体",
            "times", "timesnewroman", "arial", "calibri", "cambria",
            "microsoftyahei", "yahei", "微软雅黑",
        ],
        "line_spacing": 1.5,
        "page_number_required": False,
        "cover_required": False,
        "references_required": False,
        "reference_min_entries": 0,
    },
    # 自由格式：不做任何格式判定。
    # 用户只是想把文件打出来，不该被模板标准挑刺；也避免边距/页码规则误伤海报、
    # 表格、手抄笔记这类本来就没有统一版式的文档。
    "free_format": {
        "id": "free_format",
        "name": "自由格式",
        "skip_checks": True,
        "page_size_required": False,
        "margins_mm": {"top": 0, "bottom": 0, "left": 0, "right": 0},
        "font_whitelist": [],
        "line_spacing": None,
        "page_number_required": False,
        "cover_required": False,
        "references_required": False,
        "reference_min_entries": 0,
    },
    # 学位论文：参考 GB/T 7713.1 与多数高校本硕论文格式要求，装订侧留 30mm
    "gb_thesis": {
        "id": "gb_thesis",
        "name": "学位论文模板（国标）",
        "margins_mm": {"top": 25, "bottom": 25, "left": 30, "right": 20},
        "font_whitelist": [
            "simsun", "songti", "stsong", "nsimsun", "宋体",
            "simhei", "heiti", "stheiti", "黑体",
            "kaiti", "stsong", "楷体",
            "times", "timesnewroman", "arial", "calibri", "cambria",
            "microsoftyahei", "yahei", "微软雅黑",
        ],
        "line_spacing": 1.5,
        "page_number_required": True,
        "cover_required": True,
        "references_required": True,
        "reference_min_entries": 10,
    },
    # 实验报告：多数高校实验报告只强调装订边，不强制封面与参考文献
    "lab_report": {
        "id": "lab_report",
        "name": "实验报告模板",
        "margins_mm": {"top": 25, "bottom": 25, "left": 25, "right": 20},
        "font_whitelist": [
            "simsun", "songti", "stsong", "nsimsun", "宋体",
            "simhei", "heiti", "stheiti", "黑体",
            "times", "timesnewroman", "arial", "calibri", "cambria",
            "microsoftyahei", "yahei", "微软雅黑",
        ],
        "line_spacing": 1.5,
        "page_number_required": False,
        "cover_required": False,
        "references_required": False,
        "reference_min_entries": 0,
    },
    # 简历：边距偏小、行距自由，不检查页码/封面/参考文献（行距为 None 表示不判定）
    "resume": {
        "id": "resume",
        "name": "简历模板",
        "margins_mm": {"top": 12, "bottom": 12, "left": 12, "right": 12},
        "font_whitelist": [
            "simsun", "songti", "stsong", "nsimsun", "宋体",
            "simhei", "heiti", "黑体",
            "times", "timesnewroman", "arial", "calibri", "cambria",
            "microsoftyahei", "yahei", "微软雅黑",
            "helvetica", "georgia", "garamond",
        ],
        "line_spacing": None,
        "page_number_required": False,
        "cover_required": False,
        "references_required": False,
        "reference_min_entries": 0,
    },
    # 公文：GB/T 9704-2012 党政机关公文格式，上 37 / 下 35 / 左 28 / 右 26
    "official_doc": {
        "id": "official_doc",
        "name": "公文模板（GB/T 9704）",
        "margins_mm": {"top": 37, "bottom": 35, "left": 28, "right": 26},
        "font_whitelist": [
            "fangsong", "仿宋", "stsong", "宋体", "simsun",
            "simhei", "heiti", "黑体",
            "kaiti", "楷体",
            "xiaobiaosong", "小标宋",
            "timesnewroman", "times",
        ],
        "line_spacing": 1.75,
        "page_number_required": True,
        "cover_required": False,
        "references_required": False,
        "reference_min_entries": 0,
    },
}


def _has_margin_rule(tpl):
    """模板是否规定了页边距；全为 0（自由格式）时不判定、也不自动缩放。"""
    margins = (tpl or {}).get("margins_mm") or {}
    return any(float(value or 0) > 0 for value in margins.values())


def _load_templates():
    data = {key: dict(value) for key, value in BUILTIN_TEMPLATES.items()}
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    if not os.path.isdir(template_dir):
        return data
    for filename in sorted(os.listdir(template_dir)):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(template_dir, filename), "r", encoding="utf-8") as f:
                template = json.load(f)
            key = template.get("id") or filename[:-5]
            if template.get("name") and template.get("margins_mm"):
                data[key] = template
        except Exception:
            # 单个模板坏掉不影响其他模板
            continue
    return data


TEMPLATES = _load_templates()


def get_template(name):
    return TEMPLATES.get(name) or TEMPLATES.get("general_thesis") or BUILTIN_TEMPLATES["general_thesis"]


def _is_blank(page):
    if page.get_text().strip():
        return False
    if page.get_images():
        return False
    if page.get_drawings():
        return False
    return True


def _is_scanned_page(page):
    """没有文字层、但有图片：大概率是扫描件。"""
    return len(page.get_text().strip()) < 5 and bool(page.get_images())


def _in_body_zone(page, rect):
    """页眉/页脚区域：上下各 8%。"""
    height = page.rect.height
    center = (rect.y0 + rect.y1) / 2
    return height * 0.08 <= center <= height * 0.92


def _content_rect(page):
    """正文内容外接框；忽略页码块和页眉/页脚里的短文本/小图标。"""
    boxes = []
    for block in page.get_text("blocks"):
        text = (block[4] or "").strip()
        if not text:
            continue
        rect = fitz.Rect(block[:4])
        if PAGE_NUMBER_RE.fullmatch(text):
            continue
        if len(text) <= 60 and not _in_body_zone(page, rect):
            continue
        boxes.append(rect)
    try:
        for info in page.get_image_info():
            rect = fitz.Rect(info["bbox"])
            if not _in_body_zone(page, rect) and rect.get_area() < page.rect.get_area() * 0.25:
                continue
            boxes.append(rect)
    except Exception:
        pass
    if not boxes:
        return None
    rect = boxes[0]
    for box in boxes[1:]:
        rect |= box
    return rect


def _margins_mm(page):
    rect = _content_rect(page)
    if rect is None:
        return None
    return {
        "left": max(rect.x0, 0) / MM,
        "top": max(rect.y0, 0) / MM,
        "right": max(page.rect.width - rect.x1, 0) / MM,
        "bottom": max(page.rect.height - rect.y1, 0) / MM,
    }


def _norm_font(name):
    name = (name or "").split("+", 1)[-1].lower()
    return re.sub(r"[\s_\-]", "", name)


def _font_allowed(name, whitelist):
    low = _norm_font(name)
    return any(_norm_font(word) in low for word in whitelist if word)


def _font_usage(page):
    """只统计正文区域里的字体，按字符数累计；装饰字体不会因为出现一次就报警。"""
    usage = {}
    try:
        for block in page.get_text("dict").get("blocks", []):
            rect = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
            if not _in_body_zone(page, rect):
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text") or ""
                    if not text.strip():
                        continue
                    font = span.get("font") or ""
                    usage[font] = usage.get(font, 0) + len(text)
    except Exception:
        pass
    return usage


def _line_spacing_ratios(page):
    """用基线间距 / 字号计算行距；跳过表格、多栏、行首位置差异大的块。"""
    ratios = []
    try:
        for block in page.get_text("dict").get("blocks", []):
            rect = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
            if not _in_body_zone(page, rect):
                continue
            lines = block.get("lines", [])
            if len(lines) < 2:
                continue
            xs = [line["bbox"][0] for line in lines]
            if max(xs) - min(xs) > page.rect.width * 0.15:
                continue
            origins = []
            sizes = []
            for line in lines:
                spans = [span for span in line.get("spans", [])
                         if (span.get("text") or "").strip()]
                if not spans:
                    continue
                origin = spans[0].get("origin") or (0, line["bbox"][3])
                origins.append(origin[1])
                sizes.append(max(float(span.get("size") or 0) for span in spans))
            for index in range(1, len(origins)):
                gap = origins[index] - origins[index - 1]
                size = sizes[index - 1] or 1
                if gap > 0:
                    ratios.append(gap / size)
    except Exception:
        pass
    return ratios


def _has_page_number(page):
    height = page.rect.height
    for block in page.get_text("blocks"):
        text = (block[4] or "").strip()
        if not text or len(text) > 24:
            continue
        if PAGE_NUMBER_RE.fullmatch(text) and (
                block[3] > height * 0.85 or block[1] < height * 0.12):
            return True
    return False


def _cover_present(page):
    """首页：大字、行数少、没有页码，且不是摘要/目录页。"""
    max_size = 0.0
    text_len = 0
    line_count = 0
    text_parts = []
    try:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                line_count += 1
                for span in line.get("spans", []):
                    max_size = max(max_size, float(span.get("size") or 0))
                    text = span.get("text") or ""
                    text_len += len(text)
                    text_parts.append(text)
    except Exception:
        pass
    head = "".join(text_parts)[:200]
    if re.match(r"^\s*(摘要|abstract|目录|contents)", head, re.I):
        return False
    return max_size >= 18 and line_count <= 8 and text_len <= 300 and not _has_page_number(page)


def _reference_stats(doc, checked):
    """返回 (是否有参考文献标题, 条目数)。只做粗略统计，格式正确性不判定。"""
    heading_found = False
    entries = set()
    pattern = re.compile(r"^\s*(\[\s*\d+\s*\]|［\s*\d+\s*］|\d+[\.、])\s*\S+")
    for index in range(checked):
        text = doc.load_page(index).get_text()
        if re.search(r"参考文献|references", text, re.I):
            heading_found = True
        for line in text.splitlines():
            if pattern.match(line.strip()):
                entries.add(line.strip()[:80])
    return heading_found, len(entries)


def analyze(path, template_name="general_thesis", max_pages=50):
    tpl = get_template(template_name)
    doc = fitz.open(path)
    issues = []
    try:
        total = doc.page_count
        checked = min(total, max_pages)

        if tpl.get("skip_checks"):
            # 自由格式：不用模板标准去评判本来就没有统一版式的文档，只如实回报页数
            scanned_pages = sum(1 for index in range(checked)
                                if _is_scanned_page(doc.load_page(index)))
            return {
                "template": tpl["name"],
                "template_key": template_name,
                "pages": total,
                "checked_pages": checked,
                "truncated": total > checked,
                "issues": [],
                "issue_count": 0,
                "errors": 0,
                "warnings": 0,
                "score": 100,
                "scanned": (scanned_pages / checked > 0.5) if checked else False,
                "scanned_pages": scanned_pages,
                "ocr_available": False,
                "cover_present": False,
                "references_found": False,
                "reference_entries": 0,
                "fonts": [],
                "line_spacing_avg": None,
                "confidence_counts": {"high": 0, "medium": 0, "low": 0},
                "margin_bad": {},
                "free_format": True,
            }

        expected = tpl["margins_mm"]
        margin_rule = _has_margin_rule(tpl)
        page_size_required = bool(tpl.get("page_size_required", True))
        font_usage = {}
        spacing_ratios = []
        missing_pn = []
        margin_bad = {}
        scanned_pages = 0

        for index in range(checked):
            page = doc.load_page(index)
            width, height = page.rect.width, page.rect.height
            scanned = _is_scanned_page(page)
            if scanned:
                scanned_pages += 1

            if page_size_required and (abs(width - 595.28) > 6 or abs(height - 841.89) > 6):
                issues.append({
                    "level": "warn", "code": "page_size", "page": index + 1,
                    "confidence": "high",
                    "message": f"第 {index + 1} 页尺寸 {width:.0f}×{height:.0f}pt，不是 A4",
                })

            margins = _margins_mm(page)
            if margin_rule and margins and not scanned:
                for side, label in (("top", "上"), ("bottom", "下"),
                                    ("left", "左"), ("right", "右")):
                    if margins[side] + 2.0 < expected[side]:
                        margin_bad.setdefault(side, []).append(index + 1)
                        issues.append({
                            "level": "warn", "code": "margin", "page": index + 1,
                            "confidence": "high",
                            "message": (f"第 {index + 1} 页{label}边距 "
                                        f"{margins[side]:.1f}mm，建议 ≥{expected[side]}mm"),
                        })

            if not scanned:
                usage = _font_usage(page)
                for font, chars in usage.items():
                    font_usage[font] = font_usage.get(font, 0) + chars
                spacing_ratios.extend(_line_spacing_ratios(page))
                if (tpl["page_number_required"] and index > 0
                        and not _has_page_number(page) and not _is_blank(page)):
                    missing_pn.append(index + 1)

        scanned_ratio = scanned_pages / checked if checked else 0
        scanned_doc = scanned_ratio > 0.5
        ocr_available = False
        if scanned_doc:
            try:
                import ocr as ocr_module
                ocr_available = ocr_module.available()
            except Exception:
                ocr_available = False
            issues.append({
                "level": "warn", "code": "scanned",
                "confidence": "high",
                "message": ("文档以扫描图片为主，字体/行距/页码检查不可靠；"
                            + ("可点击「OCR 识别」补充页码/参考文献检查"
                               if ocr_available
                               else "建议上传文字版 PDF，或安装 Tesseract + 中文语言包")),
            })

        total_chars = sum(font_usage.values())
        significant_fonts = {
            font: chars for font, chars in font_usage.items()
            if chars >= max(20, total_chars * 0.05)
        }
        # 模板不给字体白名单（如简历）时，字体不作判定
        whitelist = tpl.get("font_whitelist") or []
        unknown_fonts = sorted(
            name for name in significant_fonts
            if not _font_allowed(name, whitelist)) if whitelist else []
        if unknown_fonts:
            issues.append({
                "level": "info", "code": "font", "confidence": "medium",
                "message": "正文主要字体不在模板白名单：" + "、".join(unknown_fonts[:6]),
            })

        avg_spacing = None
        if spacing_ratios:
            ordered = sorted(spacing_ratios)
            avg_spacing = ordered[len(ordered) // 2]
        # 模板没给行距标准（如简历）时只回报实测值，不判合规
        spacing_target = tpl.get("line_spacing")
        if avg_spacing is not None and spacing_target:
            if avg_spacing < 1.1:
                issues.append({
                    "level": "warn", "code": "line_spacing", "confidence": "medium",
                    "message": f"正文行距约 {avg_spacing:.2f} 倍，偏小（建议约 {tpl['line_spacing']} 倍）",
                })
            elif avg_spacing > 3.0:
                issues.append({
                    "level": "warn", "code": "line_spacing", "confidence": "medium",
                    "message": f"正文行距约 {avg_spacing:.2f} 倍，偏大（建议约 {tpl['line_spacing']} 倍）",
                })

        if tpl["page_number_required"] and missing_pn and not scanned_doc:
            issues.append({
                "level": "warn", "code": "page_number", "confidence": "high",
                "message": f"{len(missing_pn)} 页缺少页码：第 " + "、".join(map(str, missing_pn[:8])) + " 页",
            })

        heading_found, entry_count = _reference_stats(doc, checked)
        if tpl["references_required"] and not scanned_doc:
            if not heading_found:
                issues.append({
                    "level": "warn", "code": "references", "confidence": "low",
                    "message": "未检测到「参考文献 / References」章节",
                })
            elif entry_count < int(tpl.get("reference_min_entries") or 0):
                issues.append({
                    "level": "warn", "code": "references", "confidence": "low",
                    "message": f"参考文献条目约 {entry_count} 条，少于模板要求；格式正确性需人工复核",
                })

        cover_present = _cover_present(doc.load_page(0)) if total else False
        if tpl["cover_required"] and not scanned_doc and not cover_present:
            issues.append({
                "level": "warn", "code": "cover", "confidence": "low",
                "message": "首页未检测到封面特征（大字标题/行数少/无页码），可能缺少封面",
            })

        errors = sum(1 for issue in issues if issue["level"] == "error")
        warnings = sum(1 for issue in issues if issue["level"] == "warn")
        confidence_counts = {"high": 0, "medium": 0, "low": 0}
        for issue in issues:
            confidence_counts[issue.get("confidence", "medium")] += 1
        score = max(0, 100 - errors * 10 - warnings * 3)
        return {
            "template": tpl["name"],
            "template_key": template_name,
            "pages": total,
            "checked_pages": checked,
            "truncated": total > checked,
            "issues": issues[:30],
            "issue_count": len(issues),
            "errors": errors,
            "warnings": warnings,
            "score": score,
            "scanned": scanned_doc,
            "scanned_pages": scanned_pages,
            "ocr_available": ocr_available,
            "cover_present": cover_present,
            "references_found": heading_found,
            "reference_entries": entry_count,
            "fonts": sorted(significant_fonts),
            "line_spacing_avg": None if avg_spacing is None else round(avg_spacing, 2),
            "confidence_counts": confidence_counts,
            "margin_bad": {key: value[:10] for key, value in margin_bad.items()},
        }
    finally:
        doc.close()


def _cjk_font():
    """找一个可用的中文字体文件；找不到返回 None（退回内置 china-s）。"""
    candidates = [
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _add_cover(doc, title, author="", course=""):
    page = doc.new_page(width=595.28, height=841.89)
    font_path = _cjk_font()
    fontname = "china-s"
    if font_path:
        try:
            page.insert_font(fontname="cjk", fontfile=font_path)
            fontname = "cjk"
        except Exception:
            fontname = "china-s"
    lines = [
        (title or "文档", 200, 26),
        (author or "", 280, 15),
        (course or "", 320, 13),
    ]
    for text, y, size in lines:
        if not text:
            continue
        try:
            page.insert_text((80, y), text, fontname=fontname, fontsize=size)
        except Exception:
            page.insert_text((80, y), text.encode("ascii", "ignore").decode() or "Document",
                             fontname="helv", fontsize=size)
    return page


def _needs_margin_fix(page, tpl):
    if _is_scanned_page(page):
        return False
    # 模板没有页边距标准（自由格式）时不做缩放，否则会把内容硬塞进 0 边距框里
    if not _has_margin_rule(tpl):
        return False
    margins = _margins_mm(page)
    if margins is None:
        return False
    expected = tpl["margins_mm"]
    return any(margins[side] + 2.0 < expected[side]
               for side in ("top", "bottom", "left", "right"))


def fix(path, out_path, options=None, template_name="general_thesis"):
    tpl = get_template(template_name)
    opts = {
        "remove_blank": True,
        "fix_margins": True,
        "add_page_numbers": True,
        "insert_cover": False,
        "duplex": False,
        "author": "",
        "course": "",
    }
    opts.update(options or {})

    src = fitz.open(path)
    dst = fitz.open()
    try:
        pages = list(range(src.page_count))
        if opts["remove_blank"]:
            pages = [i for i in pages if not _is_blank(src.load_page(i))]
        removed = src.page_count - len(pages)

        if opts["insert_cover"]:
            title = os.path.splitext(os.path.basename(path))[0]
            _add_cover(dst, title, opts.get("author") or "", opts.get("course") or "")

        margins = {key: value * MM for key, value in tpl["margins_mm"].items()}
        scaled = 0
        for index in pages:
            page = src.load_page(index)
            if opts["fix_margins"] and _needs_margin_fix(page, tpl):
                new_page = dst.new_page(width=page.rect.width, height=page.rect.height)
                target = fitz.Rect(
                    margins["left"], margins["top"],
                    page.rect.width - margins["right"],
                    page.rect.height - margins["bottom"],
                )
                new_page.show_pdf_page(target, src, index)
                scaled += 1
            else:
                dst.insert_pdf(src, from_page=index, to_page=index)

        numbered = 0
        if opts["add_page_numbers"]:
            start = 1 if opts["insert_cover"] else 0
            total_numbered = max(len(dst) - start, 1)
            for index in range(start, len(dst)):
                page = dst.load_page(index)
                if _has_page_number(page):
                    continue
                text = f"{index - start + 1} / {total_numbered}"
                width = fitz.get_text_length(text, fontname="helv", fontsize=9)
                page.insert_text(
                    ((page.rect.width - width) / 2, page.rect.height - 20),
                    text, fontname="helv", fontsize=9, color=(0, 0, 0))
                numbered += 1

        parity_blank = False
        if opts["duplex"] and len(dst) % 2 == 1:
            last = dst.load_page(len(dst) - 1)
            dst.new_page(width=last.rect.width, height=last.rect.height)
            parity_blank = True

        dst.save(out_path, garbage=4, deflate=True)
        return {
            "original_pages": src.page_count,
            "new_pages": len(dst),
            "removed_blank": removed,
            "margin_fixed": scaled,
            "page_numbers_added": numbered,
            "cover_added": bool(opts["insert_cover"]),
            "duplex_parity_blank": parity_blank,
            "template": tpl["name"],
        }
    finally:
        src.close()
        dst.close()
