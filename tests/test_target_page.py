"""target_page 目标页判定测试（T19，真 chromium）。

背景：sheng 真机 8 标签页实测，grab 取 pages[-1] 落在空白新标签页，
红框注入了人看不到的页。本文件用真 chromium 验证 pick_target_page 的
可观测判定：剔空白页 → visibilityState 选人正看着的前台页 → 逐级回落
→ 一页没有给人话 RuntimeError。

真 chromium 直启（不走 CDP 守护）：pick_target_page 只吃带 .pages 的
上下文，playwright 自家 context 即真实形状；页与页的 visibilityState
在 headless 下通常全 "visible"（无窗口管理器遮挡），故"后台页不可见"
的场景按契约用 page_visibility 注入点（monkeypatch）构造。
"""

from __future__ import annotations

import logging

import pytest

from pageplay import target_page
from pageplay.target_page import (
    BLANK_PREFIXES,
    is_blank,
    page_visibility,
    pick_target_page,
)


# ---------------------------------------------------------------------------
# 真 chromium 夹具（模块级起一次；每个测试各用独立 context 隔离）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_browser():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    yield browser
    browser.close()
    pw.stop()


def _goto(real_browser, flow_site: str, path: str):
    """新 context 开一页导航到 flow_site 路径，返回 (context, page)。"""
    context = real_browser.new_context()
    page = context.new_page()
    page.goto(flow_site + path)
    return context, page


def _new_blank_page(context):
    """开一个空白标签页：先试 chrome://new-tab-page，打不开就保持
    new_page 自带的 about:blank——两者都落 BLANK_PREFIXES。"""
    page = context.new_page()
    try:
        page.goto("chrome://new-tab-page")
    except Exception:
        pass  # 内核不给开 chrome:// 时保持 about:blank，同为空白页
    return page


# ---------------------------------------------------------------------------
# 纯判定：is_blank / page_visibility 异常路径（不需要浏览器）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["chrome://new-tab-page", "about:blank",
                                 "devtools://devtools/bundled/inspector.html",
                                 ""])
def test_is_blank_prefixes_and_empty(url):
    """空白前缀（chrome:///about:/devtools://）与空 URL 都算空白页。"""
    assert is_blank(type("P", (), {"url": url})()) is True


def test_is_blank_content_url_false():
    assert is_blank(type("P", (), {"url": "http://127.0.0.1:9/list"})()) is False
    assert BLANK_PREFIXES == ("chrome://", "about:", "devtools://")


class _Boom:
    """evaluate 必抛的替身（等价"页已被关"）。"""

    @staticmethod
    def evaluate(_js: str):
        raise RuntimeError("Target closed")


def test_page_visibility_exception_maps_hidden():
    assert page_visibility(_Boom()) == "hidden"  # 问不动 = 不可见，不炸


# ---------------------------------------------------------------------------
# 三页场景（两个真实内容页 + 一个空白新标签页，sheng 真机等价小场景）
# ---------------------------------------------------------------------------

def test_three_pages_skips_blank_picks_last_content(real_browser, flow_site):
    """headless 默认页全 visible：跳过末尾空白页，选最后一个内容页。

    即 sheng 真机场景的等价复现（空白新标签页排最后 + 内容页在前）。
    即便某些环境把后台页标 hidden，两条路径对该布局结果相同。
    """
    ctx_a, start = _goto(real_browser, flow_site, "/start")
    ctx_b, listing = _goto(real_browser, flow_site, "/list")
    try:
        blank = _new_blank_page(ctx_b)
        assert is_blank(blank) is True
        picked = pick_target_page(ctx_b)  # ctx_b：[列表页, 空白页]
        assert picked is listing
        assert picked.url.endswith("/list")
        # headless 下真实前台页就是 visible（真实函数未打桩）
        assert page_visibility(listing) == "visible"
        assert start.url.endswith("/start")  # 哨兵：另一 context 未被碰
    finally:
        ctx_a.close()
        ctx_b.close()


def test_middle_page_visible_wins_over_position(real_browser, flow_site,
                                                monkeypatch):
    """中间页 visible 其余 hidden：选中间页（可见性压过位置顺序）。"""
    context, start = _goto(real_browser, flow_site, "/start")
    try:
        listing = context.new_page()
        listing.goto(flow_site + "/list")
        blank = _new_blank_page(context)
        monkeypatch.setattr(
            target_page, "page_visibility",
            lambda page: "visible" if page.url.endswith("/list") else "hidden")
        picked = pick_target_page(context)
        assert picked is listing  # 中间页，而非最后的空白页/首个内容页
        assert start.url.endswith("/start")
    finally:
        context.close()


def test_first_page_visible_wins_over_last_position(real_browser, flow_site,
                                                    monkeypatch):
    """仅首页 visible：选首页——证明"取 visible"不被 pages[-1] 位置带跑。"""
    context, start = _goto(real_browser, flow_site, "/start")
    try:
        listing = context.new_page()
        listing.goto(flow_site + "/list")
        monkeypatch.setattr(
            target_page, "page_visibility",
            lambda page: "visible" if page.url.endswith("/start") else "hidden")
        assert pick_target_page(context) is start
    finally:
        context.close()


def test_all_hidden_falls_back_to_last_non_blank(real_browser, flow_site,
                                                 monkeypatch):
    """全 hidden（无可观测前台页）：回落非空白页最后一个。"""
    context, start = _goto(real_browser, flow_site, "/start")
    try:
        listing = context.new_page()
        listing.goto(flow_site + "/list")
        blank = _new_blank_page(context)
        monkeypatch.setattr(target_page, "page_visibility",
                            lambda page: "hidden")
        picked = pick_target_page(context)
        assert picked is listing          # 非空白页最后一个
        assert picked is not blank and picked is not start
    finally:
        context.close()


def test_multiple_visible_logs_take_last_note(real_browser, flow_site,
                                              monkeypatch, caplog):
    """多页同时可见：取最后一个，且 log.info 说明"取最后一个"+页标识。"""
    context, start = _goto(real_browser, flow_site, "/start")
    try:
        listing = context.new_page()
        listing.goto(flow_site + "/list")
        monkeypatch.setattr(target_page, "page_visibility",
                            lambda page: "visible")  # 全可见（含未打桩时）
        with caplog.at_level(logging.INFO, logger="pageplay.target_page"):
            picked = pick_target_page(context)
        assert picked is listing          # visible 里取最后一个
        assert "最后一个" in caplog.text
        assert "/list" in caplog.text     # 选中的页进日志（url 前 60 字）
    finally:
        context.close()


def test_eight_pages_blank_last_mirrors_real_device(real_browser, flow_site):
    """sheng 真机 8 标签页场景等价复现：7 个内容页 + 末尾空白新标签页
    → 选第 7 个内容页，绝不落空白页。"""
    context, first = _goto(real_browser, flow_site, "/start")
    try:
        content = [first]
        for i in range(6):
            page = context.new_page()
            page.goto(flow_site + ["/list", "/other"][i % 2])
            content.append(page)
        blank = _new_blank_page(context)  # 第 8 页：空白新标签页
        assert len(context.pages) == 8
        picked = pick_target_page(context)
        assert picked is content[-1]      # 最后一个内容页
        assert picked is not blank
        assert not is_blank(picked)
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 空页人话：只有空白页 / 一页都没有
# ---------------------------------------------------------------------------

def test_only_blank_pages_raises_human_error(real_browser, flow_site):
    """内容页一个没有（只有空白新标签页）：人话 RuntimeError。

    空白页不算"可用标签页"（向它注入红框正是 T19 要修的病），不兜底
    返回空白页——与既有 grab 零页人话口径（"先打开窗口"）一致。
    """
    context = real_browser.new_context()
    try:
        blank = _new_blank_page(context)
        with pytest.raises(RuntimeError, match="标签页"):
            pick_target_page(context)
        # 人话里带"先打开窗口"指引（与 grab 既有零页口径一致）
        with pytest.raises(RuntimeError, match="先打开窗口"):
            pick_target_page(context)
        assert is_blank(blank)
    finally:
        context.close()


def test_zero_pages_raises_human_error(real_browser):
    """连页都没有：人话 RuntimeError，不静默回落。"""
    context = real_browser.new_context()
    try:
        with pytest.raises(RuntimeError, match="先打开窗口"):
            pick_target_page(context)
    finally:
        context.close()
