"""印前内容审核。

两条通道：
1. DeepSeek（优先）：提取 PDF 文本层，交给 deepseek-flash 判断是否适合公开；
2. GLM（回退）：把页面渲染成图片做视觉审核。

边界说明（不编造）：
- DeepSeek 只审文本，扫描件没有文字层时返回 unknown 并转人工，不假装通过；
- 身份证号/手机号由 pii.py 在本地识别，不发给云端；
- 两个 Key 都未配置时返回 available=False；
- 调用失败如实写入 reason，不把失败当 pass。
"""

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor

import fitz
import urllib.error
import urllib.request

import setting

PROMPT = (
    "你是自助打印机的印前内容审核员。判断这一页纸张内容是否存在风险：\n"
    "1. 色情或低俗\n2. 暴力或恐怖\n3. 涉政敏感\n"
    "4. 明显侵权（如带他人版权水印的商业素材）\n5. 违法信息（赌博、毒品、诈骗等）\n\n"
    "只输出 JSON，不要任何解释：\n"
    '{"verdict":"pass|warn|block","categories":[],"reason":"不超过40字"}\n'
    "pass=正常文档；warn=轻微擦边或无法确定；block=明确违规。\n"
    "注意：教材、论文、合同、简历、证件复印件、身份证复印件都属于正常内容，不要误判。"
)

CATEGORY_ORDER = ["色情低俗", "暴力恐怖", "涉政敏感", "侵权", "违法信息"]
MAX_WIDTH = 900
SINGLE_TIMEOUT = 25

DEEPSEEK_PROMPT = (
    "你是共享资料库的内容审核员，判断这份文档是否适合公开展示和打印。\n"
    "只检查文本：色情低俗、暴力恐怖、涉政敏感、明显侵权（盗版教材/他人版权）、"
    "违法信息（赌博/毒品/诈骗）、个人隐私（身份证号/手机号/家庭住址）。\n"
    "只输出一个 JSON 对象，不要解释、不要 Markdown：\n"
    '{"verdict":"pass|warn|block","categories":[],"reason":"不超过40字"}\n'
    "pass=正常；warn=擦边/含隐私/无法确定；block=明确违规。\n"
    "教材、论文、合同、简历、证件复印件属于正常内容，不要误判。"
)


def _render_data_url(page):
    zoom = MAX_WIDTH / max(page.rect.width, 1.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    png = pix.tobytes("png")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _extract_json(text):
    """模型可能包 ```json 代码块，取第一个完整 JSON 对象。"""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _review_page(data_url):
    """单页送审，返回 {verdict, categories, reason} 或 {error}。"""
    payload = {
        "model": setting.GLM_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": PROMPT},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 512,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    request = urllib.request.Request(
        setting.GLM_URL, data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Authorization", "Bearer " + setting.GLM_API_KEY)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=SINGLE_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return {"error": f"HTTP {exc.code}"}
        message = (detail.get("error") or {}).get("message") if isinstance(
            detail.get("error"), dict) else detail.get("message")
        return {"error": f"{exc.code} {message or ''}".strip()}
    except Exception as exc:
        return {"error": str(exc)}

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"error": "响应格式异常"}
    parsed = _extract_json(content)
    if parsed is None:
        return {"error": "模型未返回可解析的 JSON"}
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "warn", "block"):
        return {"error": f"未知结论 {verdict}"}
    categories = [c for c in (parsed.get("categories") or []) if isinstance(c, str)]
    return {
        "verdict": verdict,
        "categories": categories,
        "reason": str(parsed.get("reason", ""))[:120],
    }


def _extract_text(path, pages):
    """提取前 N 页文本层，截断到配置上限。"""
    doc = fitz.open(path)
    try:
        texts = []
        for index in range(min(doc.page_count, pages)):
            texts.append(doc.load_page(index).get_text("text"))
        return "\n".join(texts)[:max(setting.DEEPSEEK_MAX_CHARS, 1000)]
    finally:
        doc.close()


def _review_text_deepseek(text):
    """DeepSeek 文本审核，返回 {verdict, categories, reason} 或 {error}。"""
    payload = {
        "model": setting.DEEPSEEK_MODEL,
        "messages": [{
            "role": "user",
            "content": DEEPSEEK_PROMPT + "\n\n文档文本：\n" + text,
        }],
        "temperature": 0.1,
        # deepseek-flash 默认会先输出推理内容，预留足够 token 才能拿到 JSON
        "max_tokens": 2000,
    }
    request = urllib.request.Request(
        setting.DEEPSEEK_URL, data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Authorization", "Bearer " + setting.DEEPSEEK_API_KEY)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=setting.DEEPSEEK_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return {"error": f"HTTP {exc.code}"}
        message = (detail.get("error") or {}).get("message") if isinstance(
            detail.get("error"), dict) else detail.get("message")
        return {"error": f"{exc.code} {message or ''}".strip()}
    except Exception as exc:
        return {"error": str(exc)}

    try:
        content = body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return {"error": "响应格式异常"}
    parsed = _extract_json(content)
    if parsed is None:
        return {"error": "模型未返回可解析的 JSON"}
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "warn", "block"):
        return {"error": f"未知结论 {verdict}"}
    categories = [c for c in (parsed.get("categories") or []) if isinstance(c, str)]
    return {
        "verdict": verdict,
        "categories": categories,
        "reason": str(parsed.get("reason", ""))[:120],
    }


def _review_with_deepseek(path):
    """提取文本层后用 DeepSeek 审核；扫描件无文本时如实转人工。"""
    doc = fitz.open(path)
    try:
        total_pages = doc.page_count
    finally:
        doc.close()
    pages = min(total_pages, max(1, setting.GLM_REVIEW_PAGES))
    text = _extract_text(path, pages)
    base = {
        "available": True,
        "model": setting.DEEPSEEK_MODEL,
        "pages": pages,
        "page_count": total_pages,
        "errors": 0,
        "detail": [],
    }
    if not text.strip():
        base.update({
            "verdict": "unknown",
            "categories": [],
            "reason": "扫描件没有文字层，DeepSeek 无法审核，转人工",
        })
        return base
    result = _review_text_deepseek(text)
    if "error" in result:
        base.update({
            "verdict": "unknown",
            "categories": [],
            "reason": "DeepSeek 调用失败：" + result["error"],
        })
        return base
    base.update({
        "verdict": result["verdict"],
        "categories": result["categories"],
        "reason": result["reason"],
    })
    return base


def review(path):
    """审核文档前 GLM_REVIEW_PAGES 页，返回汇总结果。"""
    if (setting.DEEPSEEK_API_KEY or "").strip():
        return _review_with_deepseek(path)
    if not (setting.GLM_API_KEY or "").strip():
        return {"available": False,
                "message": "内容审核未接入（缺少 DEEPSEEK_API_KEY / GLM_API_KEY）"}

    doc = fitz.open(path)
    total_pages = doc.page_count
    pages = min(total_pages, max(1, setting.GLM_REVIEW_PAGES))
    images = []
    for index in range(pages):
        page = doc.load_page(index)
        images.append(_render_data_url(page))
    doc.close()

    with ThreadPoolExecutor(max_workers=min(5, len(images))) as pool:
        raw_results = list(pool.map(_review_page, images))

    detail = []
    categories = []
    errors = 0
    for index, item in enumerate(raw_results, start=1):
        if "error" in item:
            errors += 1
            detail.append({"page": index, "verdict": "unknown", "reason": item["error"]})
            continue
        detail.append({"page": index, "verdict": item["verdict"],
                       "categories": item["categories"], "reason": item["reason"]})
        for name in item["categories"]:
            if name not in categories:
                categories.append(name)

    verdicts = [d["verdict"] for d in detail]
    if "block" in verdicts:
        verdict = "block"
    elif "warn" in verdicts:
        verdict = "warn"
    elif errors == pages:
        verdict = "unknown"
    elif errors:
        verdict = "warn"
    else:
        verdict = "pass"

    if verdict == "unknown":
        reason = "审核调用失败，未能判断（请稍后重试）"
    elif verdict == "pass":
        reason = "前 %d 页未发现违规内容" % pages
    else:
        hits = [d for d in detail if d["verdict"] in ("warn", "block")]
        reason = "；".join(
            f"第{d['page']}页：{d.get('reason') or '存在风险'}" for d in hits[:3])

    return {
        "available": True,
        "verdict": verdict,
        "categories": categories,
        "reason": reason,
        "model": setting.GLM_MODEL,
        "pages": pages,
        "page_count": total_pages,
        "errors": errors,
        "detail": detail,
    }
