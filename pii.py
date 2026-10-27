"""打印前隐私检查：识别身份证号、手机号、银行卡号、住址，并支持一键打码。

只做本地规则匹配（PyMuPDF 文本层），不调用外部 AI，文档不出服务器。
扫描件没有文字层，识别不到，返回 scanned=True 让前端如实提示，不假装检查通过。
"""

import re

import fitz

ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 允许 13800138000 / 138 0013 8000 / 138-0013-8000 三种写法
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d(?:[ \-]?\d{4}){2}(?!\d)")
# 16–19 位卡号，允许每 4 位用空格或短横分隔
BANK_RE = re.compile(r"(?<!\d)(?:\d{4}[ \-]?){3}\d{4}(?:[ \-]?\d{1,3})?(?!\d)")
# 中文住址：至少到区/县，再出现路/街/巷 + 号/村/镇
ADDR_RE = re.compile(
    r"(?:[\u4e00-\u9fa5]{2,10}(?:省|自治区|特别行政区))?"
    r"(?:[\u4e00-\u9fa5]{2,10}(?:市|自治州|地区|盟))?"
    r"[\u4e00-\u9fa5]{2,10}(?:区|县|旗)"
    r"[\u4e00-\u9fa5A-Za-z0-9\s\-]{1,20}?"
    r"(?:路|街|道|巷|胡同|弄)"
    r"[\u4e00-\u9fa5A-Za-z0-9\s\-]{0,15}?"
    r"(?:号|村|镇)"
    r"[\u4e00-\u9fa5A-Za-z0-9\s\-]{0,20}"
)
# 单份文档最多检查的页数，避免超大文件阻塞请求
MAX_PAGES = 50


def _luhn_ok(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _valid_bank(value):
    """银行卡号：16–19 位、常见卡 BIN 开头；Luhn 通过或带分组分隔符均可。"""
    digits = re.sub(r"\D", "", value)
    if len(digits) not in (16, 17, 18, 19) or digits[:1] not in ("3", "4", "5", "6"):
        return False
    return _luhn_ok(digits) or bool(re.search(r"[ \-]", value))


def _find_spans(text):
    """返回 [(start, end, 类型)]，前面的命中区间会把重叠项排除。"""
    spans = []

    def taken(start, end):
        return any(start < b and end > a for a, b, _ in spans)

    for match in ID_RE.finditer(text):
        spans.append((match.start(), match.end(), "身份证号"))
    for match in BANK_RE.finditer(text):
        if taken(match.start(), match.end()) or not _valid_bank(match.group()):
            continue
        spans.append((match.start(), match.end(), "银行卡号"))
    for match in PHONE_RE.finditer(text):
        if taken(match.start(), match.end()):
            continue
        spans.append((match.start(), match.end(), "手机号"))
    for match in ADDR_RE.finditer(text):
        if taken(match.start(), match.end()):
            continue
        spans.append((match.start(), match.end(), "住址"))
    spans.sort()
    return spans


def _iter_lines(page):
    """按行拼接 span 文本，避免号码被拆成多个 span 后识别不到。"""
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                yield text, fitz.Rect(line.get("bbox"))


def _mask(value, kind):
    """返回值只用于前端展示，保留头尾便于用户确认是哪一条。"""
    if kind == "身份证号" and len(value) >= 10:
        return value[:3] + "*" * (len(value) - 7) + value[-4:]
    if kind == "手机号" and len(value) >= 8:
        return value[:3] + "****" + value[-4:]
    if kind == "银行卡号":
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 8:
            return digits[:4] + " **** " + digits[-4:]
    if kind == "住址" and len(value) > 6:
        return value[:6] + "****"
    return "*" * len(value)


def _hit_rects(page, value, line_rect):
    """优先精确命中文本，找不到时退化为整行，确保不会漏打码。"""
    rects = page.search_for(value, clip=line_rect)
    return rects or [line_rect]


def scan(path):
    """扫描 PDF 文本层，返回检出项与统计。"""
    doc = fitz.open(path)
    items = []
    seen = set()
    text_pages = 0
    pages = min(doc.page_count, MAX_PAGES)
    for index in range(pages):
        page = doc.load_page(index)
        if page.get_text("text").strip():
            text_pages += 1
        for text, line_rect in _iter_lines(page):
            for start, end, kind in _find_spans(text):
                value = text[start:end]
                for rect in _hit_rects(page, value, line_rect):
                    key = (index, kind, round(rect.x0, 1), round(rect.y0, 1),
                           round(rect.x1, 1), round(rect.y1, 1))
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append({
                        "page": index + 1,
                        "type": kind,
                        "masked": _mask(value, kind),
                        "rect": [round(rect.x0, 1), round(rect.y0, 1),
                                 round(rect.x1, 1), round(rect.y1, 1)],
                    })
    total_pages = doc.page_count
    doc.close()
    counts = {}
    for item in items:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {
        "total": len(items),
        "by_type": counts,
        "items": items[:50],
        "scanned": text_pages == 0,
        "checked_pages": pages,
        "page_count": total_pages,
        "truncated": total_pages > pages,
    }


def redact(src, dst):
    """把身份证号、手机号、银行卡号、住址覆盖成黑块，结果写入 dst。"""
    doc = fitz.open(src)
    count = 0
    touched = set()
    pages = min(doc.page_count, MAX_PAGES)
    for index in range(pages):
        page = doc.load_page(index)
        rects = []
        seen = set()
        for text, line_rect in _iter_lines(page):
            for start, end, _kind in _find_spans(text):
                for rect in _hit_rects(page, text[start:end], line_rect):
                    key = (round(rect.x0, 1), round(rect.y0, 1),
                           round(rect.x1, 1), round(rect.y1, 1))
                    if key in seen:
                        continue
                    seen.add(key)
                    rects.append(rect)
        for rect in rects:
            page.add_redact_annot(rect, fill=(0, 0, 0))
        if rects:
            page.apply_redactions()
            count += len(rects)
            touched.add(index + 1)
    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return {"redacted": count, "pages": sorted(touched)}
