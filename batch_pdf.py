"""班级/社团批量：生成「姓名封皮 + 正文」的待打印 PDF。

为什么在服务端拼文件：打印机端只认「一个文件 + 页码范围 + 份数」，
把 N 份（每份前加一张姓名封皮）拼成一个 PDF 后，
Windows(SumatraPDF) 和 Linux(ipptool) 两条打印链路都不用改。
"""

import datetime

import fitz

MARGIN = 60.0
GRAY = (0.45, 0.45, 0.45)
FAINT = (0.8, 0.82, 0.85)

# 中文用宋体、拉丁/数字用比例字体：CJK 内置字体把 ASCII 当全角，
# 直接混排会出现「S i l n k」「2 0 2 6」的空隙，故按字符分别取字体。
_FONT_CJK = fitz.Font("china-s")
_FONT_LAT = fitz.Font("helv")


def _size(height, ratio, lo, hi):
    """按页面高度取字号，兼顾 A4 / A3，并夹在合理区间。"""
    return min(max(height * ratio, lo), hi)


def _runs(text):
    """切成「CJK 段 / 拉丁段」，分别用对应字体量宽与绘制。"""
    runs, cur, cur_cjk = [], "", None
    for ch in text:
        cjk = ord(ch) > 0x2E80
        if cur and cjk != cur_cjk:
            runs.append((cur, cur_cjk))
            cur = ""
        cur += ch
        cur_cjk = cjk
    if cur:
        runs.append((cur, cur_cjk))
    return runs


def _width(text, size):
    return sum((_FONT_CJK if cjk else _FONT_LAT).text_length(seg, size)
               for seg, cjk in _runs(text))


def _center(page, y, text, size, color=(0, 0, 0), max_width=None, bold=False,
            gray=False):
    """居中写一行；超宽自动缩字号；bold 用轻微描边加粗。"""
    if not text:
        return
    if gray:
        color = GRAY
    if max_width:
        while size > 7 and _width(text, size) > max_width:
            size -= 0.5
    cursor = (page.rect.width - _width(text, size)) / 2
    for seg, cjk in _runs(text):
        font = _FONT_CJK if cjk else _FONT_LAT
        name = "china-s" if cjk else "helv"
        for dx in ((0, 0.4) if bold else (0,)):
            page.insert_text((cursor + dx, y), seg, fontname=name,
                             fontsize=size, color=color)
        cursor += font.text_length(seg, size)


def _rule(page, y, span, color=FAINT, width=0.8, dashes=None):
    left = (page.rect.width - span) / 2
    page.draw_line(fitz.Point(left, y), fitz.Point(left + span, y),
                   color=color, width=width, dashes=dashes)


def _cover(out, width, height, seq, copies, name, filename, content_pages,
           order_no, owner, duplex, skipped=0,
           title="班级 / 社团批量打印"):
    """插入一张正式文档封面（居中标题 + 姓名 + 明细 + 装订线 + 页脚）。"""
    page = out.new_page(width=width, height=height)
    usable = width - MARGIN * 2

    # 装订线：左侧留白内的浅灰竖虚线，方便装订对齐
    bind_x = MARGIN * 0.5
    page.draw_line(fitz.Point(bind_x, height * 0.12),
                   fitz.Point(bind_x, height * 0.9),
                   color=FAINT, width=0.8, dashes="[3 3] 0")

    # 顶部：品牌单位 + 分隔线
    _center(page, height * 0.115, "思印客  Silnk  ·  自助打印终端",
            _size(height, 0.014, 10, 13), gray=True, max_width=usable)
    _rule(page, height * 0.145, min(usable, width * 0.5))

    # 中部：标题 + 份次
    _center(page, height * 0.3, title, _size(height, 0.038, 22, 34),
            bold=True, max_width=usable)
    _center(page, height * 0.345, f"第 {seq} 份 / 共 {copies} 份",
            _size(height, 0.016, 11, 14), gray=True, max_width=usable)

    # 姓名：分拣关键，单列突出
    _center(page, height * 0.47, "姓    名", _size(height, 0.015, 11, 13),
            gray=True)
    _center(page, height * 0.525, name or "（未填姓名）",
            _size(height, 0.05, 26, 40), bold=True, max_width=usable)

    # 明细：居中逐行
    info = []
    if filename:
        info.append(f"文件：{filename}")
    note = f"正文 {content_pages} 页 · {'双面' if duplex else '单面'}"
    if skipped:
        note += f" · 已跳过 {skipped} 张空白页"
    info.append(note)
    meta = []
    if order_no:
        meta.append(f"订单尾号 {order_no[-6:]}")
    if meta:
        info.append("   ".join(meta))
    if owner:
        info.append(f"组织者：{owner}")
    info_size = _size(height, 0.016, 11, 13)
    step = height * 0.03
    for index, line in enumerate(info):
        _center(page, height * 0.645 + index * step, line, info_size,
                max_width=usable)

    # 页脚：分拣说明 + 日期
    _center(page, height - 108,
            "统一取件：请整叠交给组织者；如需分发，按本页姓名分拣。",
            _size(height, 0.012, 9, 11), gray=True, max_width=usable)
    _rule(page, height - 74, min(usable, width * 0.5))
    today = datetime.date.today()
    _center(page, height - 46,
            f"思印客  Silnk       {today.year} 年 {today.month} 月 {today.day} 日",
            _size(height, 0.012, 9, 11), gray=True, max_width=usable)


def build(src_path, out_path, names, copies, start, end,
          order_no="", owner="", filename="", duplex=False,
          skip_pages=(), mode="unified"):
    """拼出可直接打印的批量 PDF。

    参数：
        names   —— 名单（可少于份数，缺的封皮写「未填姓名」）
        copies  —— 份数
        start/end —— 正文页码范围（1 起，含两端）
        duplex  —— 双面时封皮背面留白，保证每份正文从正面开始
        skip_pages —— 空白页页码（1 起），不打印也不收费
        mode    —— unified 班级/社团批量（同一文件印多份）；
                   group 拼单（每人一份自己的文件，也要姓名封皮便于分拣）
    返回：
        (总页数, 每份实际正文页数)

    取件码不打印在纸上（每页与封皮都不印），只在用户端订单详情展示。
    """
    title = ("拼单打印 · 按姓名分拣" if mode == "group"
             else "班级 / 社团批量打印")
    if copies < 1:
        raise ValueError("copies must be >= 1")
    if end < start:
        raise ValueError("invalid page range")

    skip = {int(p) for p in (skip_pages or [])}
    src = fitz.open(src_path)
    out = fitz.open()
    try:
        if src.page_count < end:
            raise ValueError("page range exceeds pdf pages")
        content_page_numbers = [p for p in range(start, end + 1) if p not in skip]
        content_pages = len(content_page_numbers)
        if content_pages == 0:
            raise ValueError("no printable pages")
        skipped = (end - start + 1) - content_pages
        base = src.load_page(start - 1).rect
        width, height = base.width, base.height
        clean_names = [str(n).strip() for n in (names or [])]

        for index in range(copies):
            name = clean_names[index] if index < len(clean_names) else ""
            _cover(out, width, height, index + 1, copies, name, filename,
                   content_pages, order_no, owner, duplex,
                   skipped=skipped, title=title)
            if duplex:
                # 封皮背面留白：下一张纸才是正文正面
                out.new_page(width=width, height=height)
            for page_no in content_page_numbers:
                rect = src.load_page(page_no - 1).rect
                page = out.new_page(width=rect.width, height=rect.height)
                page.show_pdf_page(page.rect, src, page_no - 1)
            if duplex and content_pages % 2:
                # 正文奇数页：补一张空白背面，保证下一份从正面开始
                out.new_page(width=width, height=height)

        out.save(out_path, garbage=3, deflate=True)
        return out.page_count, content_pages
    finally:
        src.close()
        out.close()
