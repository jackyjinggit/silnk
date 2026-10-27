"""A3 拆 A4：把 A3 页面按方向切成两张 A4，方便只有 A4 纸的打印机输出。

- 横向 A3（420×297mm）默认左右切，得到两张 A4 纵向。
- 纵向 A3（297×420mm）默认上下切，得到两张 A4 横向。
- 混排文档只处理 A3 页，其他页原样保留。
- 使用 PDF 矢量复制，不转图片，文字仍然清晰、可搜索。
"""

import fitz

A4_W, A4_H = 595.28, 841.89       # A4 纵向（pt）
A3_W, A3_H = 841.89, 1190.55      # A3 纵向（pt）
TOLERANCE = 0.03                  # 尺寸容差 3%，兼容 842×1191 之类的取整


def is_a3(page):
    """按长短边判断是否 A3，纵向/横向都算。"""
    width, height = page.rect.width, page.rect.height
    long_side, short_side = max(width, height), min(width, height)
    return (abs(long_side - A3_H) / A3_H <= TOLERANCE
            and abs(short_side - A3_W) / A3_W <= TOLERANCE)


def a3_pages_in_doc(doc):
    """返回 A3 页码列表（从 1 开始）。"""
    return [index + 1 for index in range(doc.page_count)
            if is_a3(doc.load_page(index))]


def a3_pages(path):
    doc = fitz.open(path)
    try:
        return a3_pages_in_doc(doc)
    finally:
        doc.close()


def split(path, out_path, direction="auto"):
    """把 path 拆成 out_path，返回统计信息。

    direction: auto（按页面方向）/ vertical（左右）/ horizontal（上下）。
    """
    direction = (direction or "auto").strip().lower()
    if direction not in ("auto", "vertical", "horizontal"):
        direction = "auto"

    src = fitz.open(path)
    out = fitz.open()
    stats = {"original_pages": src.page_count, "a3_pages": 0,
             "new_pages": 0, "direction": direction}
    try:
        for index in range(src.page_count):
            page = src.load_page(index)
            # 旋转页先用 remove_rotation 把 /Rotate 烘进内容，
            # 这样后面的 clip 坐标就是用户实际看到的版式，不会切错边。
            if page.rotation:
                page.remove_rotation()
            if not is_a3(page):
                out.insert_pdf(src, from_page=index, to_page=index)
                continue

            stats["a3_pages"] += 1
            width, height = page.rect.width, page.rect.height

            mode = direction
            if mode == "auto":
                mode = "vertical" if width > height else "horizontal"

            if mode == "vertical":
                clips = [fitz.Rect(0, 0, width / 2, height),
                         fitz.Rect(width / 2, 0, width, height)]
                target_w, target_h = A4_W, A4_H
            else:
                clips = [fitz.Rect(0, 0, width, height / 2),
                         fitz.Rect(0, height / 2, width, height)]
                target_w, target_h = A4_H, A4_W

            for clip in clips:
                new_page = out.new_page(width=target_w, height=target_h)
                new_page.show_pdf_page(
                    fitz.Rect(0, 0, target_w, target_h),
                    src, index, clip=clip, keep_proportion=True)

        stats["new_pages"] = out.page_count
        stats["split_mode"] = direction
        out.save(out_path, garbage=3, deflate=True)
    finally:
        out.close()
        src.close()
    return stats
