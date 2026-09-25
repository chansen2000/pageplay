"""目标页判定：附着后选「人正看着的可见标签页」（T19）。

sheng 真机 8 标签页实测：grab 取 pages[-1]（"最近打开 = 正在看"的
位置假设），注入落在一个空白新标签页上，红框人根本看不到。本模块把
"当前页"从位置假设改成可观测判定，供 grab/pick 取页共用：

1. 剔空白页：URL 以 chrome:// / about: / devtools:// 开头或为空；
2. 问每页 document.visibilityState —— 窗口里只有前台标签答 "visible"
   （人正看着的那页），有 visible 取最后一个；
3. 都不可见（替身环境/非常规窗口）→ 退取非空白页最后一个；
4. 连非空白页都没有（只有空白页 / 一页都没有）→ 人话 RuntimeError
   （"没有可用的标签页"——向空白新标签页注入红框正是 T19 要修的病，
   不做"兜底取空白页"，与 grab 既有零页人话口径一致）。

判定只读不动页：不导航、不关页、不改焦点（人看着哪页就哪页）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

BLANK_PREFIXES = ("chrome://", "about:", "devtools://")


def is_blank(page) -> bool:
    """URL 以空白前缀开头或为空 → 空白页（chrome://new-tab-page 属空白）。"""
    url = str(getattr(page, "url", "") or "")
    return (not url) or url.startswith(BLANK_PREFIXES)


def page_visibility(page) -> str:
    """问页面 document.visibilityState；问不动（页已被关等）按 hidden。

    测试 monkeypatch 注入点：真浏览器里只有前台标签答 "visible"，
    后台标签与不可见环境答 "hidden"。
    """
    try:
        return str(page.evaluate("document.visibilityState") or "hidden")
    except Exception:
        return "hidden"  # evaluate 抛（页被关/替身没有该方法）= 不可见


def pick_target_page(context):
    """从附着上下文选目标页：人正看着的可见标签页优先（T19 契约）。

    visible 非空取其最后一个（同屏多窗时最近的前台页）；没有可见页
    退取非空白页最后一个；只有空白页 / 一页都没有 → 人话 RuntimeError。
    选中即 log.info（title/url 前 60 字）供真机排查。
    """
    pages = list(getattr(context, "pages", None) or [])
    candidates = [p for p in pages if not is_blank(p)]
    if not candidates:  # 一页没有 = 全是空白页：都叫"没有可用的标签页"
        raise RuntimeError("浏览器里没有可用的标签页：先打开窗口并浏览到目标页")
    visible = [p for p in candidates if page_visibility(p) == "visible"]
    if visible:
        if len(visible) > 1:
            log.info("目标页：%d 个可见标签页，取最后一个", len(visible))
        page = visible[-1]
    else:
        page = candidates[-1]
    title = ""
    try:
        title = str(page.title() or "")
    except Exception:
        pass  # 个别内建页 title() 会失败，不让它挡选页
    log.info("目标页选定：%s（%s）", title[:60] or "（无标题）", page.url[:60])
    return page
