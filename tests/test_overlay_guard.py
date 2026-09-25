"""T20 框选防呆测试：SEM 收窄 + 红框类型标签 + 锁定面板守卫（真 chromium）。

消灭"抓表 0 行"误报的 UI 半场：hover 阶段就分清锁中的是什么（标签三态），
ul/ol 不再被语义优先劫持（卡片组走字段勾选），锁定后读不到列/无表格
结构的目标不给可确认的抓表入口。模拟"人"不用第二线程碰 playwright
（sync API 禁跨线程）：页面内预埋轮询脚本，覆层出现后向目标派发合成
mousemove/click——走真实 hover/锁定/面板链路；点不了确认（置灰/无按钮）
就派 Esc 收场，等价真人的"看一眼 → 换目标"。内核缺失如实 skip 不假绿。
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay import picker  # noqa: E402
from pageplay.picker import PickCancelled, run_pick  # noqa: E402

_TIMEOUT = 20  # 兜底超时：覆层/绑定缺席快速失败，不等 600s

# ul 里 3 张同签名 li 卡片 + 真表格 + 空表格 + role=grid(无 tr) + 普通块
_HTML = """<html><body>
<h1>守卫测试页</h1>
<ul id="cards">
  <li class="item">卡片一 <span>10 元</span></li>
  <li class="item">卡片二 <span>20 元</span></li>
  <li class="item">卡片三 <span>30 元</span></li>
</ul>
<table id="t">
  <thead><tr><th>名称</th><th>价格</th></tr></thead>
  <tbody>
    <tr><td>苹果</td><td>5.5</td></tr>
    <tr><td>香蕉</td><td>3.2</td></tr>
  </tbody>
</table>
<table id="empty"></table>
<div id="plain">一块普通内容</div>
<div role="grid" id="g"><div role="row">A</div></div>
</body></html>"""

# 替人：覆层在场 → 对目标派 mousemove（记 hover 标签）→ lock 时再派
# click（记锁定面板与按钮态）。确认按钮可点才点（确认收场）；置灰/无
# 确认按钮（守卫生效）→ 派 Esc 收场，run_pick 以 PickCancelled 返回。
_ARM = """
(args) => {
  const timer = setInterval(() => {
    if (!window.__pageplay_onconfirm
        || !document.getElementById("__pageplay_overlay")) return;
    clearInterval(timer);
    const el = document.querySelector(args.target);
    el.dispatchEvent(new MouseEvent("mousemove", {bubbles: true}));
    window.__pp_hover_label =
      document.getElementById("__pageplay_boxlabel").textContent;
    if (args.lock) el.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    const overlay = document.getElementById("__pageplay_overlay");
    window.__pp_lock_label =
      document.getElementById("__pageplay_boxlabel").textContent;
    const panel = overlay.children[1];
    window.__pp_panel_text = panel.textContent;
    window.__pp_buttons = Array.prototype.map.call(
      panel.querySelectorAll("button"),
      b => ({text: b.textContent, disabled: b.disabled}));
    const ok = Array.prototype.find.call(
      panel.querySelectorAll("button"), b => b.textContent === "确认");
    if (ok && !ok.disabled) { ok.click(); return; }
    document.dispatchEvent(
      new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
  }, 25);
}
"""


@pytest.fixture
def page(monkeypatch):
    """真 chromium headless 页（铺守卫测试页）；内核缺失如实 skip。"""
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _TIMEOUT)
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"playwright chromium 不可用，跳过框选守卫：{exc}")
    pg = browser.new_page()
    pg.set_content(_HTML)
    yield pg
    pg.close()
    browser.close()
    pw.stop()


def _buttons(page) -> list[dict]:
    return page.evaluate("() => window.__pp_buttons")


def _run(page, target: str, lock: bool):
    """替人 hover（可选锁定）跑一轮 run_pick；守卫拦住确认时返回 None。"""
    page.evaluate(_ARM, {"target": target, "lock": lock})
    try:
        return run_pick(page, lambda _r: None)
    except PickCancelled:
        return None  # 确认被守卫拦住（置灰/无按钮）→ Esc 收场的路径


# ----------------------------------------------------------------------
# SEM 收窄：ul 卡片组不再被语义劫持 → 字段勾选条（而非列勾选）
# ----------------------------------------------------------------------

def test_ul_card_group_gets_field_bar_not_column_bar(page):
    """hover li：红框落卡片组父容器，标签"卡片列表：3 条记录"，锁定出
    字段勾选面板（list_mode=cards）——ul 不再被 extract_table 路径劫持。"""
    result = _run(page, "#cards .item", lock=True)

    assert page.evaluate("() => window.__pp_hover_label") == "卡片列表：3 条记录"
    assert "识别到 3 条记录" in page.evaluate("() => window.__pp_panel_text")
    assert result is not None and result["list_mode"] == "cards"
    assert result["fields"]  # 字段候选非空（字段路径可用）


# ----------------------------------------------------------------------
# 红框类型标签三态 + 锁定后保留
# ----------------------------------------------------------------------

def test_label_table_state_with_row_count(page):
    """hover td → 语义表格优先：标签"表格：3 行"，锁定出列勾选条。"""
    result = _run(page, "#t td", lock=True)

    assert page.evaluate("() => window.__pp_hover_label") == "表格：3 行"
    assert "已锁定表格" in page.evaluate("() => window.__pp_panel_text")
    assert result is not None and result["columns"] == ["名称", "价格"]


def test_label_plain_element_state_and_no_grab_button(page):
    """hover 普通块：标签"元素（无表格结构，仅可下载）"；锁定面板只有
    下载，不渲染"抓取此表"，给一行灰字说明。"""
    _run(page, "#plain", lock=True)

    assert page.evaluate(
        "() => window.__pp_hover_label") == "元素（无表格结构，仅可下载）"
    texts = [b["text"] for b in _buttons(page)]
    assert "下载此元素" in texts and "抓取此表" not in texts
    assert "该元素无表格结构，不可抓表" in page.evaluate(
        "() => window.__pp_panel_text")


def test_label_survives_lock(page):
    """锁定后标签保留（不是 hover 一次性）：锁定态仍显示类型与行数。"""
    _run(page, "#t td", lock=True)

    assert page.evaluate("() => window.__pp_lock_label") == "表格：3 行"


# ----------------------------------------------------------------------
# 守卫：读不到列 / 无表格结构 → 不给可确认的抓表入口
# ----------------------------------------------------------------------

def test_empty_table_confirm_disabled_with_red_note(page):
    """锁中无 tr 的空表格：确认置灰 + 红字"读不到列"，确认被拦。"""
    result = _run(page, "#empty", lock=True)

    assert page.evaluate("() => window.__pp_hover_label") == "表格：0 行"
    assert "该目标读不到列" in page.evaluate("() => window.__pp_panel_text")
    ok = next(b for b in _buttons(page) if b["text"] == "确认")
    assert ok["disabled"] is True
    assert result is None  # 确认不可达：Esc 收场（不存在 0 行确认出口）


def test_role_grid_without_tr_confirm_disabled(page):
    """role=grid 无 tr：语义容器照样锁中，但列读不到 → 确认置灰拦住。"""
    result = _run(page, "#g", lock=True)

    assert page.evaluate("() => window.__pp_hover_label") == "表格：0 行"
    ok = next(b for b in _buttons(page) if b["text"] == "确认")
    assert ok["disabled"] is True
    assert result is None
