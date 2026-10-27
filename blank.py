"""空白页识别：不打印、不计费。

判断口径与 /api/user/check 一致：没有文字、没有图片、没有矢量图形才算空白。
只检查前 200 页，与结算的逐页分析上限保持一致，避免超大文件拖慢下单。
"""

import fitz

MAX_SKIP_PAGES = 200


def find_blank_pages(path, start, end):
    """返回 [start, end] 范围内的空白页码（1 起）。

    只扫描文档前 200 页，与 /api/user/check 的检查窗口保持一致，
    保证前端预估和后端结算用同一份空白页数据。
    """
    doc = fitz.open(path)
    try:
        total = doc.page_count
        start = max(1, min(int(start), total))
        end = max(start, min(int(end), total))
        scan_end = min(end, total, MAX_SKIP_PAGES)
        pages = []
        for page_no in range(1, scan_end + 1):
            if page_no < start:
                continue
            page = doc.load_page(page_no - 1)
            if (not page.get_text().strip()
                    and not page.get_images()
                    and not page.get_drawings()):
                pages.append(page_no)
        return pages
    finally:
        doc.close()
