"""把用户选择页范围复制成新的打印文件。

不修改上传的原文件：预览、格式检查、二次下单都仍看到原样。
取件码不再打印在纸上（避免浪费墨粉、也便于纸张复用），只在用户端订单详情展示。
"""

import fitz


def build(src_path, out_path, start, end, skip_pages=()):
    """返回新文件的页数；skip_pages 里的页码（1 起）不打印。"""
    if end < start:
        raise ValueError("invalid page range")
    skip = {int(p) for p in (skip_pages or [])}
    src = fitz.open(src_path)
    out = fitz.open()
    try:
        if src.page_count < end:
            raise ValueError("page range exceeds pdf pages")
        for page_no in range(start - 1, end):
            if page_no + 1 in skip:
                continue
            source = src.load_page(page_no)
            page = out.new_page(width=source.rect.width, height=source.rect.height)
            page.show_pdf_page(page.rect, src, page_no)
        out.save(out_path, garbage=3, deflate=True)
        return out.page_count
    finally:
        src.close()
        out.close()
