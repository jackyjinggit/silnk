"""页面排版：把多页文档合并到一张 A4 上（2 / 4 / 6 页每张）。

只做版面拼合，不改变页面内容；矢量内容保持矢量，不转图片，文字仍可搜索。
输出固定 A4 纵向：
- 2 页/张：1 列 2 行
- 4 页/张：2 列 2 行
- 6 页/张：2 列 3 行
原页按比例缩放并居中，四周留出装订/裁切余量。
"""

import fitz

A4_W, A4_H = 595.28, 841.89
MARGIN = 14.0
GUTTER = 8.0
GRIDS = {2: (1, 2), 4: (2, 2), 6: (2, 3)}


def nup(src, dst, per_sheet=4):
    """把 src 按 per_sheet 页合并到 A4，写入 dst，返回页数统计。"""
    per_sheet = int(per_sheet)
    if per_sheet not in GRIDS:
        raise ValueError("per_sheet must be 2, 4 or 6")
    cols, rows = GRIDS[per_sheet]
    cell_w = (A4_W - 2 * MARGIN - (cols - 1) * GUTTER) / cols
    cell_h = (A4_H - 2 * MARGIN - (rows - 1) * GUTTER) / rows

    doc = fitz.open(src)
    out = fitz.open()
    try:
        original_pages = doc.page_count
        for start in range(0, original_pages, per_sheet):
            page = out.new_page(width=A4_W, height=A4_H)
            for slot in range(per_sheet):
                index = start + slot
                if index >= original_pages:
                    break
                source = doc.load_page(index)
                width = source.rect.width or 1
                height = source.rect.height or 1
                scale = min(cell_w / width, cell_h / height)
                draw_w = width * scale
                draw_h = height * scale
                row, col = divmod(slot, cols)
                x0 = MARGIN + col * (cell_w + GUTTER) + (cell_w - draw_w) / 2
                y0 = MARGIN + row * (cell_h + GUTTER) + (cell_h - draw_h) / 2
                target = fitz.Rect(x0, y0, x0 + draw_w, y0 + draw_h)
                page.show_pdf_page(target, doc, index)
        out.save(dst, garbage=3, deflate=True)
        return {
            "per_sheet": per_sheet,
            "original_pages": original_pages,
            "new_pages": out.page_count,
        }
    finally:
        out.close()
        doc.close()
