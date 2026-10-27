"""按页色彩覆盖率分档计价。

规则（前端 /api/user/check 与后端结算共用，避免两边算不一致）：
- 覆盖率 < 0.5%：视为无彩色，按黑白价
- 0.5% ~ 5%：低覆盖，彩色价 × 0.5
- 5% ~ 30%：中覆盖，彩色价 × 0.8
- > 30%：全彩，彩色价 × 1.0
- 双面按一张纸两面的平均单价，再乘该机的双面/单面价比例
"""

import fitz

COV_NO_COLOR = 0.005
COV_LOW_MAX = 0.05
COV_MID_MAX = 0.30
COV_LOW_FACTOR = 0.5
COV_MID_FACTOR = 0.8
# 结算时最多逐页分析 200 页，超过的部分按全彩价（防止超大文件把下单请求拖死）
MAX_PRICING_PAGES = 200


def coverage_config():
    """返回给前端的配置（阈值用百分比，便于展示）。"""
    return {
        "no_color_percent": COV_NO_COLOR * 100,
        "low_max_percent": COV_LOW_MAX * 100,
        "mid_max_percent": COV_MID_MAX * 100,
        "low_factor": COV_LOW_FACTOR,
        "mid_factor": COV_MID_FACTOR,
    }


def tier_of(coverage):
    """返回 none / low / mid / high。"""
    if coverage < COV_NO_COLOR:
        return "none"
    if coverage <= COV_LOW_MAX:
        return "low"
    if coverage <= COV_MID_MAX:
        return "mid"
    return "high"


def page_color_coverage(page, zoom=50.0 / 72.0):
    """渲染一页并统计彩色像素占比（0~1）。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    samples = pix.samples
    step = 3 * 4  # 每 4 个像素抽一个
    checked = 0
    colored = 0
    for i in range(0, len(samples) - 2, step):
        r, g, b = samples[i], samples[i + 1], samples[i + 2]
        checked += 1
        if max(r, g, b) - min(r, g, b) > 30 and max(r, g, b) > 60:
            colored += 1
    return (colored / checked) if checked else 0.0


def analyze_range(path, start, end):
    """返回 [start, end] 范围内每页的彩色覆盖率（0~1）。

    超过 MAX_PRICING_PAGES 的页按 1.0（全彩）处理，保证列表长度与页数一致，
    避免超大文件逐页渲染把下单请求拖太久。
    """
    doc = fitz.open(path)
    try:
        total = doc.page_count
        start = max(1, min(int(start), total))
        end = max(start, min(int(end), total))
        values = []
        for page_no in range(start, end + 1):
            # 前端 /api/user/check 只分析文档前 200 页；这里用同一窗口，
            # 第 200 页之后按全彩价，保证「前端显示价 = 后端结算价」。
            if page_no > MAX_PRICING_PAGES:
                values.append(1.0)
                continue
            values.append(page_color_coverage(doc.load_page(page_no - 1)))
        return values
    finally:
        doc.close()


def single_page_price(coverage, bw_price, color_price):
    """单面一页的价格；纯黑白页按黑白价。"""
    if coverage is None or coverage < COV_NO_COLOR:
        return bw_price
    if coverage <= COV_LOW_MAX:
        return color_price * COV_LOW_FACTOR
    if coverage <= COV_MID_MAX:
        return color_price * COV_MID_FACTOR
    return color_price


def price_file(pdf_path, start, end, copies, side, color, skip_blank, cfg):
    """统一计价：一个文件的实付金额、省下金额、计费页数与耗纸张数。

    口径与账单结算完全一致（单面逐页、双面按张、空白页不计费、
    彩色按覆盖率分档）。用户端预估、拼单结算、/pay/bill/create 三处
    都调用本函数，避免各写一遍导致价格不一致。
    cfg 为这台机器的生效价 {bw, color, bw_duplex, color_duplex}。
    所选范围内没有可打印内容时返回 None。
    """
    import blank  # 局部导入，避免与 blank 模块互相引用时的初始化顺序问题

    start = int(start)
    end = int(end)
    copies = max(int(copies), 1)
    original_count = end - start + 1
    bw = cfg["bw"]
    color_price = cfg["color"]
    bw_duplex = cfg["bw_duplex"]
    color_duplex = cfg["color_duplex"]

    # 空白页：不打印、不计费（口径见 blank.find_blank_pages）
    blank_pages = []
    if skip_blank and pdf_path:
        try:
            blank_pages = blank.find_blank_pages(pdf_path, start, end)
        except Exception:
            blank_pages = []
    blank_set = set(blank_pages)
    chargeable_count = sum(
        1 for p in range(start, end + 1) if p not in blank_set)
    if chargeable_count == 0:
        return None

    # 彩色按「每页色彩覆盖率」分档：把空白页从计价序列里剔除
    coverage_values = None
    if color == "color" and pdf_path:
        try:
            values = analyze_range(pdf_path, start, end)
            if len(values) == original_count:
                coverage_values = [
                    v for i, v in enumerate(values)
                    if (start + i) not in blank_set
                ]
        except Exception:
            coverage_values = None

    if side == 'one-sided':
        sheets_per_copy = chargeable_count
        if color == "color":
            if coverage_values is not None:
                file_amount = sum(
                    single_page_price(c, bw, color_price)
                    for c in coverage_values) * copies
            else:
                file_amount = color_price * chargeable_count * copies
        else:
            file_amount = bw * chargeable_count * copies
    else:
        sheets_per_copy = (chargeable_count + 1) // 2
        if color == "color":
            if coverage_values is not None:
                total = 0.0
                for i in range(0, chargeable_count - 1, 2):
                    total += duplex_sheet_price(
                        coverage_values[i], coverage_values[i + 1],
                        bw, color_price, bw_duplex, color_duplex)
                if chargeable_count % 2:
                    total += single_page_price(
                        coverage_values[-1], bw, color_price)
                file_amount = total * copies
            elif chargeable_count % 2 == 0:
                file_amount = color_duplex * (chargeable_count // 2) * copies
            else:
                file_amount = (color_duplex * (chargeable_count // 2)
                               + color_price) * copies
        else:
            if chargeable_count % 2 == 0:
                file_amount = bw_duplex * (chargeable_count // 2) * copies
            else:
                file_amount = (bw_duplex * (chargeable_count // 2)
                               + bw) * copies

    # 「不跳空白页、不打覆盖率折扣」时的原价，用于展示本单省了多少钱
    if side == 'one-sided':
        full_amount = (color_price if color == 'color' else bw) * original_count
    elif color == 'color':
        full_amount = (color_duplex * (original_count // 2)
                       + (color_price if original_count % 2 else 0))
    else:
        full_amount = (bw_duplex * (original_count // 2)
                       + (bw if original_count % 2 else 0))
    full_amount = round(full_amount * copies, 2)
    file_amount = round(file_amount, 2)
    return {
        "amount": file_amount,
        "saved_amount": round(max(full_amount - file_amount, 0), 2),
        "chargeable_count": chargeable_count,
        "sheets_per_copy": sheets_per_copy,
        "blank_pages": blank_pages,
    }


def duplex_sheet_price(cov1, cov2, bw_price, color_price, bw_duplex, color_duplex):
    """双面一张纸的价格。

    两面都无彩色 → 黑白双面价；否则按两面单页价之和，乘以
    「双面彩色价 / (2 × 单面彩色价)」保持原有双面优惠比例。
    """
    no_color_1 = cov1 is None or cov1 < COV_NO_COLOR
    no_color_2 = cov2 is None or cov2 < COV_NO_COLOR
    if no_color_1 and no_color_2:
        return bw_duplex
    if not color_price:
        return color_duplex
    p1 = single_page_price(cov1, bw_price, color_price)
    p2 = single_page_price(cov2, bw_price, color_price)
    ratio = color_duplex / (2 * color_price)
    return (p1 + p2) * ratio
