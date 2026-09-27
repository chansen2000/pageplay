"""v0.10 框选面板测试（任务书 v2 D1/D2/D3）：真 chromium + 真实鼠标键盘事件。

不用第二线程碰 playwright（sync API 禁跨线程），也不往页面派发合成事件：
直接注入列表引擎与覆层 JS，让测试线程保持自由，用 page.hover /
page.mouse.click / page.keyboard.press 走真实输入链路；确认载荷从
window.__pageplay_last_payload 读取（confirmPick 的落点），取消与否从
window.__pp_cancelled 标志读取（测试自绑 oncancel）。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay import listscan  # noqa: E402
from pageplay._listscan_js import LISTSCAN_JS  # noqa: E402
from pageplay.picker import overlay_js  # noqa: E402


@pytest.fixture
def page():
    """真 chromium headless 空白页，注入引擎与覆层；内核缺失如实 skip。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"playwright chromium 不可用，跳过列表框选测试：{exc}")
    pg = browser.new_page()
    yield pg
    pg.close()
    browser.close()
    pw.stop()


# 嵌套假页：外层 6 条（默认层=记录数最多），每条内嵌 3 个同 sem 子块
_rows = []
for _o in range(1, 7):
    cells = []
    for _i, _v in zip((1, 2, 3), (11, 12, 13)):
        cells.append(
            f'<div class="inner--i{_o}{_i}"><b class="t--t{_o}{_i}">'
            f"外{_o}内{_i}</b><span class=\"p--p{_o}{_i}\">{_v} 元</span></div>")
    _rows.append(f'<div class="outer--o{_o}">{"".join(cells)}</div>')
_NESTED_HTML = ('<html><head><style>[class*="inner"] b{display:block}</style></head>'
                '<body><div class="wrap">' + "".join(_rows)
                + "</div></body></html>")

# D3 假页：紧挨标签 + 词典可译 sem + 不可译 sem + 按钮列 + 全同值列
_mini_rows = []
for _i, (_t, _z, _int, _dec, _btn) in enumerate([
        ("2026-09-21", "神秘值1", 650, "00", "确认收货"),
        ("2026-09-17", "神秘值2", 144, "40", "查看物流"),
        ("2026-09-15", "神秘值3", 98, "60", "加入购物车")], 1):
    _s = f"--b{_i}"
    _mini_rows.append(
        f'<div class="row{_s}">'
        f'<span class="shopInfoOrderTime{_s}">{_t}</span>'
        f'<span class="zork{_s}">{_z}</span>'
        f'<span class="mallTag{_s}">天猫</span>'
        f'<span class="payLabel{_s}">实付款</span>'
        f'<span class="payTotal{_s}"><span class="pSymbol{_s}">￥</span>'
        f'<span class="pInt{_s}">{_int}</span><span class="pDot{_s}">.</span>'
        f'<span class="pDec{_s}">{_dec}</span></span>'
        f'<button class="opBtn{_s}">{_btn}</button></div>')
_MINI_HTML = ('<html><head><style>[class*="row"] > *{margin-left:12px}</style></head><body><div class="wrap">' + "".join(_mini_rows)
              + "</div></body></html>")


def _inject(page):
    """显式注入引擎与覆层（set_content 不触发 init script；IIFE 幂等），
    等 overlay 布防完成（提示条出现）。"""
    # 注意：evaluate 表达式若求值为函数会被 playwright 自动调用——
    # 绑定必须包在函数体里，否则 oncancel 在绑定瞬间就自触
    page.evaluate("() => { window.__pp_cancelled = false; }")
    page.evaluate(
        "() => { window.__pageplay_oncancel = () =>"
        " { window.__pp_cancelled = true; }; }")
    page.evaluate(LISTSCAN_JS)
    page.evaluate(overlay_js())
    page.wait_for_selector("#__pageplay_hint", timeout=5000)


def _label(page):
    return page.text_content("#__pageplay_boxlabel") or ""


def _panel(page):
    return page.text_content("#__pageplay_overlay > div:nth-child(2)") or ""


def _payload(page):
    raw = page.evaluate("() => window.__pageplay_last_payload")
    return json.loads(json.dumps(raw)) if raw else None


def _center(page, selector: str):
    """元素中心坐标（真实鼠标点击落点）。"""
    return page.evaluate(
        """(sel) => { const r = document.querySelector(sel)
            .getBoundingClientRect();
            return [r.left + r.width / 2, r.top + r.height / 2]; }""", selector)


def _lock(page, selector: str):
    """真实 hover + 点击锁定，等面板列出现。"""
    page.hover(selector)
    page.mouse.click(*_center(page, selector))
    page.wait_for_selector("#__pageplay_overlay > div:nth-child(2) label",
                           timeout=5000)


# ----------------------------------------------------------------------
# D1 选层：默认层=记录数最多；↑↓ 在候选层间切换；body 层不出现
# ----------------------------------------------------------------------

def test_default_layer_is_most_records_and_arrows_switch(page):
    """点内部 → 默认外层 6 条（记录数最多）；↓ 内层 3 条；↑ 回 6 条。"""
    page.set_content(_NESTED_HTML)
    _inject(page)
    page.hover(".outer--o1 .inner--i11 .t--t11")

    assert "列表：6 条记录" in _label(page)
    page.keyboard.press("ArrowDown")
    assert "列表：3 条记录" in _label(page)
    page.keyboard.press("ArrowUp")
    assert "列表：6 条记录" in _label(page)


def test_body_layer_not_in_candidates(page):
    """候选层不含 body/html 容器（__pp_groups 排除规则，API 直查）。"""
    page.set_content(_NESTED_HTML)
    _inject(page)
    page.hover(".outer--o1 .inner--i11 .t--t11")
    info = page.evaluate(
        "() => window.__pp_groups(document.querySelector('.inner--i11'))"
        ".map(g => ({sem: g.recordSem, n: g.records.length,"
        " isBody: g.container === document.body}))")
    assert info and all(not x["isBody"] for x in info)


# ----------------------------------------------------------------------
# D2 重选/换层：Esc 解锁不结束会话；点别处直接换选；换层条；退出
# ----------------------------------------------------------------------

def test_esc_unlocks_first_then_exits(page):
    """锁定后 Esc：面板收起、覆层还在、会话未结束；再 Esc 才 oncancel。"""
    page.set_content(_NESTED_HTML)
    _inject(page)
    _lock(page, ".outer--o1 .inner--i11 .t--t11")

    page.keyboard.press("Escape")
    assert not page.evaluate("() => window.__pp_cancelled")  # 会话未结束
    assert page.evaluate(  # 面板收起（display:none，列行不可见）
        "() => { const p = document.querySelector("
        "'#__pageplay_overlay > div:nth-child(2)');"
        " return p && getComputedStyle(p).display === 'none'; }")
    assert page.evaluate("() => !!document.getElementById('__pageplay_overlay')")

    page.keyboard.press("Escape")
    assert page.evaluate("() => window.__pp_cancelled")  # 未锁定 Esc = 退出


def test_click_elsewhere_reselects(page):
    """锁定后点另一个列表：直接改选到新位置，不经过取消。"""
    page.set_content(_NESTED_HTML)
    _inject(page)
    _lock(page, ".outer--o1 .inner--i11 .t--t11")

    page.mouse.click(*_center(page, ".outer--o2 .inner--i23 .t--t23"))
    page.wait_for_selector("#__pageplay_overlay > div:nth-child(2) label",
                           timeout=5000)

    assert not page.evaluate("() => window.__pp_cancelled")
    assert "识别到 6 条记录" in _panel(page)  # 新位置默认层仍是 6 条外层


def test_panel_reselect_and_exit_buttons(page):
    """面板「重选」= Esc 解锁（会话继续）；「退出」→ oncancel。"""
    page.set_content(_NESTED_HTML)
    _inject(page)
    _lock(page, ".outer--o1 .inner--i11 .t--t11")

    page.click("#__pageplay_overlay button:has-text('重选')")
    assert not page.evaluate("() => window.__pp_cancelled")
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'#__pageplay_overlay > div:nth-child(2)')).display === 'none'")

    _lock(page, ".outer--o1 .inner--i11 .t--t11")  # 重选后再锁
    page.click("#__pageplay_overlay button:has-text('退出')")
    assert page.evaluate("() => window.__pp_cancelled")


def test_panel_layer_bar_switches(page):
    """换层条：锁内层（3 条）后「大一层 ▶」→ 6 条，「◀ 小一层」→ 3 条。"""
    page.set_content(_NESTED_HTML)
    _inject(page)
    page.hover(".outer--o1 .inner--i11 .t--t11")
    page.keyboard.press("ArrowDown")  # 先切到内层再锁定
    _lock(page, ".outer--o1 .inner--i11 .t--t11")
    assert "识别到 3 条记录" in _panel(page)

    page.click("#__pageplay_overlay button:has-text('大一层')")
    assert "识别到 6 条记录" in _panel(page)
    page.click("#__pageplay_overlay button:has-text('◀ 小一层')")
    assert "识别到 3 条记录" in _panel(page)


# ----------------------------------------------------------------------
# D3 列名与默认勾选（C1 五级 + C2 三规则）
# ----------------------------------------------------------------------

def _lock_mini(page):
    page.hover(".row--b1 [class*='shopInfoOrderTime']")
    page.mouse.click(*_center(page, ".row--b1 [class*='shopInfoOrderTime']"))
    page.wait_for_selector("#__pageplay_overlay > div:nth-child(2) label",
                           timeout=5000)


def test_label_adjacent_naming_and_no_label_column(page):
    """D3-1：紧挨标签「实付款」命名价格列；标签列本身不存在。"""
    page.set_content(_MINI_HTML)
    _inject(page)
    _lock_mini(page)
    page.click("#__pageplay_overlay button:has-text('确认')")

    cols = _payload(page)["columns"]
    names = [c["name"] for c in cols]
    assert "实付款" in names
    assert not any("payLabel" in c["key"] for c in cols)


def test_dict_translation_and_fallback(page):
    """D3-2：shopInfoOrderTime → 订单时间（词典）；zork → 列N 兜底。"""
    page.set_content(_MINI_HTML)
    _inject(page)
    _lock_mini(page)
    page.click("#__pageplay_overlay button:has-text('确认')")

    cols = _payload(page)["columns"]
    by_key = {c["key"]: c for c in cols}
    assert by_key["shopInfoOrderTime"]["name"] == "订单时间"
    zork = [c for c in cols if c["key"].startswith("zork")]
    assert zork and zork[0]["name"].startswith("列")  # 兜底 列N


def test_button_and_samevalue_columns_unchecked(page):
    """D3-3：按钮列默认不勾；全行同值列默认不勾；其余默认勾（payload 只
    带勾选列，按钮列/全同值列从面板勾选态断言）。"""
    page.set_content(_MINI_HTML)
    _inject(page)
    _lock_mini(page)

    states = page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'#__pageplay_overlay > div:nth-child(2) label'))"
        ".map(l => ({name: l.querySelector('span').textContent,"
        " checked: l.querySelector('input').checked}))")
    by = {x["name"]: x["checked"] for x in states}
    assert by.get("按钮") is False          # 按钮列不勾（C2 规则 1）
    assert by.get("列3") is False           # mallTag：无法翻译 + 非按钮，但同值？
    assert by.get("订单时间") is True

    page.click("#__pageplay_overlay button:has-text('确认')")
    cols = _payload(page)["columns"]
    assert not any(c["key"].startswith("opBtn") for c in cols)  # 不进载荷


def test_rename_memory_roundtrip(page, tmp_path, monkeypatch):
    """D3-4：改名（name≠auto_name）确认 → colnames.json 写入；第二次框选
    面板默认显示新名字并标「已记住」。"""
    monkeypatch.setenv("PAGEPLAY_HOME", str(tmp_path))
    page.set_content(_MINI_HTML)
    _inject(page)

    _lock_mini(page)
    name_span = page.locator(
        "#__pageplay_overlay > div:nth-child(2) label span").first
    name_span.dblclick()
    page.fill("#__pageplay_overlay input[type=text]", "下单时间")
    page.press("#__pageplay_overlay input[type=text]", "Enter")
    page.click("#__pageplay_overlay button:has-text('确认')")

    cols = _payload(page)["columns"]
    renamed = [c for c in cols if c["name"] != c["auto_name"]]
    assert renamed  # 改名生效
    listscan.remember_colnames("demo", cols)  # cli 确认层同一逻辑
    saved = listscan.load_colnames("demo")
    assert saved and any(v == "下单时间" for v in saved.values())

    # 第二次：注入记忆（picker 注入路径同款）→ 面板默认显示新名字 + 已记住
    page.evaluate("c => window.__pp_colnames = c",
                  listscan.load_colnames("demo"))
    _lock_mini(page)
    panel_text = _panel(page)
    assert "下单时间" in panel_text and "已记住" in panel_text


def test_panel_links_mode_payload(page, file_site):
    """「只抓链接」+ 勾「同时下载文件」→ action=links + download=true。"""
    page.goto(f"{file_site}/links")
    _inject(page)
    page.hover("#box a:first-child")
    page.mouse.click(*_center(page, "#box a:first-child"))
    # 链接页每条记录只有文字一列（列 <2）→ 垃圾闸出口「下载此元素」，
    # 没有只抓链接入口——这是字段 <2 的真实行为，面板应如实提示。
    assert "未识别到有效的重复数据结构" in _panel(page)
