"""流程录制层测试：merge_click_step 纯函数 + record_session 真浏览器端到端。

内核缺失时如实 pytest.skip，不假绿（同 test_picker 风格）。模拟"人"
沿用本目录惯例：不用第二线程碰 playwright（sync API 禁跨线程），在
页面里预埋轮询脚本（真导航点击、派发 P/Escape 键、覆层在场后调
__pageplay_onconfirm、window.close() 关窗），等价于真人浏览/按键/关窗。

关窗用例（window.close 模拟）只用 set_content 页面：实测 Chrome 对 goto
打开的 http 页面一律忽略脚本关窗（无论先跳 about:blank 还是
window.open('_self') 都无效），而 set_content/about:blank 形态下
window.close 有效且走同一条 Page closed 退出路径（同 test_picker_repeat）。
需真导航的用例（e2e/Esc）改用空闲超时退出——同为契约退出路径
（"≥1 步返回全部"），页面存活反而便于直接断言 sessionStorage 见证。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay import picker, recorder  # noqa: E402
from pageplay.picker import PickCancelled  # noqa: E402
from pageplay.recorder import merge_click_step, record_session  # noqa: E402

_IDLE_TIMEOUT = 2.5  # 录制空闲兜底：收摊时限兼编排卡死快速暴露
_NAV_MERGE_SEC = 1.0  # 落点补录窗口：测试里收紧（导航在数百 ms 内完成）
_PICK_TIMEOUT = 8     # 框选兜底：编排卡死快速暴露，不等 600s

# 跨文档替人编排（IIFE 兼作 init script 与当前文档手动执行）：
# /start 首访 → 点 #to-list（真导航）；/list → 按 P 进框选 → 覆层在场
# 后锁定 #t2 确认（两列）→ 等 python 清 picking 回浏览 → 点 #back 真导航
# 回 /start；回访 /start 只做见证：+300ms 覆层不在场 = picker overlay 的
# init 布防已被录制层清场（自然浏览不被截获）。
_ARM_FLOW = """
() => {
  const path = location.pathname;
  if (path === "/start") {
    if (sessionStorage.getItem("pageplay-t11-started")) {
      setTimeout(() => {
        if (!document.getElementById("__pageplay_overlay")) {
          sessionStorage.setItem("pageplay-t11-no-overlay", "1");
        }
      }, 300);
      return;
    }
    sessionStorage.setItem("pageplay-t11-started", "1");
    const t = setInterval(() => {
      if (!(window.__pageplay_step && window.__pageplay_picking === false)) return;
      clearInterval(t);
      setTimeout(() => document.getElementById("to-list").click(), 300);
    }, 25);
  } else if (path === "/list") {
    const t = setInterval(() => {
      if (!(window.__pageplay_step && window.__pageplay_picking === false)) return;
      clearInterval(t);
      setTimeout(() => {
        document.dispatchEvent(
          new KeyboardEvent("keydown", {key: "p", bubbles: true}));
        const t2 = setInterval(() => {
          if (!document.getElementById("__pageplay_overlay")) return;
          clearInterval(t2);
          window.__pageplay_locked = document.getElementById("t2");
          window.__pageplay_onconfirm({
            selector_hint: "#t2", action: "table",
            columns: ["名称", "价格"],
            rect: {x: 8, y: 40, w: 200, h: 60},
          });
          const t3 = setInterval(() => {
            if (window.__pageplay_picking) return;  // 等回浏览态
            clearInterval(t3);
            document.getElementById("back").click();
          }, 25);
        }, 25);
      }, 300);
    }, 25);
  }
}
"""

# 替人：按 P → 等覆层在场 → Esc 取消 → 等回浏览态 → 点 #to-other 继续录
_ARM_P_ESC_CLICK = """
() => {
  const t = setInterval(() => {
    if (!(window.__pageplay_step && window.__pageplay_picking === false)) return;
    clearInterval(t);
    document.dispatchEvent(new KeyboardEvent("keydown", {key: "p", bubbles: true}));
    const t2 = setInterval(() => {
      if (!document.getElementById("__pageplay_overlay")) return;
      clearInterval(t2);
      document.dispatchEvent(
        new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      const t3 = setInterval(() => {
        if (document.getElementById("__pageplay_overlay")) return;
        if (window.__pageplay_picking) return;
        clearInterval(t3);
        document.getElementById("to-other").click();
      }, 25);
    }, 25);
  }, 25);
}
"""

# 替人延时关窗（仅 set_content 页面有效，见模块 docstring）
_ARM_CLOSE_LATER = """
(args) => { setTimeout(() => window.close(), args.delayMs); }
"""

# 替人：点一次按钮（无导航，纯记账）后延时关窗
_ARM_CLICK_THEN_CLOSE = """
(args) => {
  const t = setInterval(() => {
    if (!(window.__pageplay_step && window.__pageplay_picking === false)) return;
    clearInterval(t);
    setTimeout(() => {
      document.getElementById("btn").click();
      setTimeout(() => window.close(), args.closeAfterMs);
    }, 300);
  }, 25);
}
"""


@pytest.fixture
def rec_browser():
    """无内核则跳过；有则起 headless chromium（每测一页由测试自建自收）。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过录制集成：{exc}")
    yield browser
    browser.close()
    pw.stop()


# ----------------------------------------------------------------------
# merge_click_step 纯函数
# ----------------------------------------------------------------------

def test_merge_appends_and_numbers():
    """归并为 click 步追加：no 顺延、返回新列表、键集合逐字对齐契约。"""
    steps: list[dict] = []
    p = {"kind": "click", "selector_hint": "a.go", "href": None, "url": "http://x/"}
    out1 = merge_click_step(steps, p, None)
    assert [s["no"] for s in out1] == [1]
    out2 = merge_click_step(out1, p, None)
    assert [s["no"] for s in out2] == [1, 2]
    assert set(out2[0]) == {"no", "kind", "url", "selector", "note"}
    assert out2[0]["kind"] == "click" and out2[0]["url"] == "http://x/"
    assert out2[0]["selector"] == "a.go"


def test_merge_note_href_or_landing():
    """note 记 href；final_url 给定且 ≠ 点击时 URL → 改记落点。"""
    p = {"kind": "click", "selector_hint": "a",
         "href": "http://x/list", "url": "http://x/start"}
    same = merge_click_step([], p, "http://x/start")     # 未换页：不写落点
    assert same[0]["note"] == "http://x/list"
    moved = merge_click_step([], p, "http://x/list?pa=2")  # 换页：落点更权威
    assert moved[0]["note"] == "http://x/list?pa=2"
    nohref = merge_click_step(
        [], {"kind": "click", "selector_hint": "button", "href": None,
             "url": "http://x/start"}, None)
    assert nohref[0]["note"] == ""


def test_merge_does_not_mutate_original_list():
    """纯函数：不改原表（长度/内容原样），返回的是新列表。"""
    orig = [{"no": 1, "kind": "click", "url": "u", "selector": "s", "note": ""}]
    snapshot = json.loads(json.dumps(orig))
    out = merge_click_step(orig, {"kind": "click", "selector_hint": "b",
                                  "href": None, "url": "u"}, None)
    assert orig == snapshot          # 原表原样
    assert out is not orig and len(out) == 2
    assert out[-1]["no"] == 2


# ----------------------------------------------------------------------
# record_session 真浏览器集成（flow_site，真导航场景）
# ----------------------------------------------------------------------

def test_record_session_e2e_click_pick_click(rec_browser, flow_site, monkeypatch):
    """端到端：点击→框选→点击混排成序，落点补进 note，回调按序被调。

    record_session 阻塞期间 python 无法在场断言，编排全在页面端预埋
    （_ARM_FLOW）；任何一步覆层/绑定/标记缺席都会卡到兜底超时必失败。
    结束用空闲超时（交互停了就收摊，≥1 步返回全部——契约退出路径之一；
    关窗退出由 set_content 用例专测，见模块 docstring）。
    """
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", _IDLE_TIMEOUT)
    monkeypatch.setattr(recorder, "_NAV_MERGE_SEC", _NAV_MERGE_SEC)
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _PICK_TIMEOUT)
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.add_init_script("(" + _ARM_FLOW + ")()")  # 新文档替人编排
        pg.evaluate(_ARM_FLOW)                       # 当前文档同份函数
        calls: list[tuple] = []

        def on_step(step: dict) -> None:
            calls.append(("step", step))

        def on_harvest(step: dict) -> None:
            calls.append(("harvest", step))

        steps = record_session(pg, on_step, on_harvest)

        assert [s["kind"] for s in steps] == ["click", "table", "click"]
        assert [s["no"] for s in steps] == [1, 2, 3]
        # click1：起点页点 /list 链接（真导航），落点补进 note
        assert steps[0]["selector"] == "#to-list"
        assert steps[0]["url"] == flow_site + "/start"
        assert steps[0]["note"] == flow_site + "/list"
        # table：/list 页按 P 框选 #t2，两列
        assert steps[1]["selector"] == "#t2"
        assert steps[1]["columns"] == ["名称", "价格"]
        assert steps[1]["url"] == flow_site + "/list"
        assert steps[1]["note"] == ""
        # click2：列表页点回 /start（真导航），落点补进 note
        assert steps[2]["selector"] == "#back"
        assert steps[2]["url"] == flow_site + "/list"
        assert steps[2]["note"] == flow_site + "/start"
        # on_step/on_harvest 按序被调，且收到返回列表里的同一步对象
        assert [(k, s["no"]) for k, s in calls] == \
            [("step", 1), ("harvest", 2), ("step", 3)]
        assert calls[0][1] is steps[0] and calls[1][1] is steps[1]
        # 回访 /start 覆层不在场：picker overlay 的 init 布防已被清场
        assert pg.evaluate(
            "() => sessionStorage.getItem('pageplay-t11-no-overlay')") == "1"
    finally:
        pg.close()


def test_record_session_esc_then_click_continues(rec_browser, flow_site,
                                                 monkeypatch):
    """Esc 取消框选回浏览模式，会话不结束、继续收 click。"""
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", _IDLE_TIMEOUT)
    monkeypatch.setattr(recorder, "_NAV_MERGE_SEC", _NAV_MERGE_SEC)
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _PICK_TIMEOUT)
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.evaluate(_ARM_P_ESC_CLICK)
        steps = record_session(pg, lambda _s: None, lambda _s: None)
    finally:
        pg.close()

    assert len(steps) == 1
    assert steps[0]["kind"] == "click"
    assert steps[0]["selector"] == "#to-other"
    assert steps[0]["url"] == flow_site + "/start"
    assert steps[0]["note"] == flow_site + "/other"  # href 与落点同值


def test_record_session_idle_timeout_raises(rec_browser, flow_site, monkeypatch):
    """空闲超时（无任何交互）→ PickCancelled，超时时限可 monkeypatch。"""
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", 1)
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        with pytest.raises(PickCancelled, match="空闲超时"):
            record_session(pg, lambda _s: None, lambda _s: None)
    finally:
        pg.close()


# ----------------------------------------------------------------------
# record_session 关窗退出路径（set_content 页面：window.close 有效）
# ----------------------------------------------------------------------

def test_record_session_close_with_steps_returns_all(rec_browser, monkeypatch):
    """关窗（已收 ≥1 步）→ 正常返回全部已记录步骤，不抛。"""
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", 10)
    monkeypatch.setattr(recorder, "_NAV_MERGE_SEC", _NAV_MERGE_SEC)
    pg = rec_browser.new_page()
    try:
        pg.set_content("<html><body><button id=\"btn\">记一笔</button></body></html>")
        pg.evaluate(_ARM_CLICK_THEN_CLOSE, {"closeAfterMs": 800})
        steps = record_session(pg, lambda _s: None, lambda _s: None)
    finally:
        pg.close()

    assert len(steps) == 1
    assert steps[0]["kind"] == "click"
    assert steps[0]["no"] == 1
    assert steps[0]["selector"] == "#btn"


def test_record_session_zero_step_close_raises(rec_browser, monkeypatch):
    """零步骤关窗 → PickCancelled（走既有 Page closed 退出路径）。"""
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", 10)
    pg = rec_browser.new_page()
    try:
        pg.set_content("<html><body>无人交互</body></html>")
        pg.evaluate(_ARM_CLOSE_LATER, {"delayMs": 400})
        with pytest.raises(PickCancelled, match="页面已关闭"):
            record_session(pg, lambda _s: None, lambda _s: None)
    finally:
        pg.close()


# ----------------------------------------------------------------------
# P 键守卫 + 纯框选收获（set_content 页面）
# ----------------------------------------------------------------------

def test_record_session_p_ignored_in_input(rec_browser, monkeypatch):
    """光标在输入框按 P 不进框选；回浏览态按 P 正常框选收获。"""
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", 10)
    monkeypatch.setattr(recorder, "_NAV_MERGE_SEC", _NAV_MERGE_SEC)
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _PICK_TIMEOUT)
    pg = rec_browser.new_page()
    try:
        pg.set_content("""<html><body>
        <input id="q" type="text">
        <table id="t"><thead><tr><th>X</th></tr></thead>
        <tbody><tr><td>1</td></tr></tbody></table>
        </body></html>""")
        pg.evaluate(
            """
            (args) => {
              const t = setInterval(() => {
                if (!(window.__pageplay_step
                      && window.__pageplay_picking === false)) return;
                clearInterval(t);
                document.getElementById("q").focus();
                // 输入框聚焦按 P：必须被忽略（不进框选）
                document.dispatchEvent(
                  new KeyboardEvent("keydown", {key: "p", bubbles: true}));
                setTimeout(() => {
                  document.activeElement.blur();
                  // 回浏览态再按 P：进框选并确认（download 档）
                  document.dispatchEvent(
                    new KeyboardEvent("keydown", {key: "p", bubbles: true}));
                  const t2 = setInterval(() => {
                    if (!document.getElementById("__pageplay_overlay")) return;
                    clearInterval(t2);
                    window.__pageplay_locked = document.getElementById("t");
                    window.__pageplay_onconfirm({
                      selector_hint: "#t", action: "download", columns: null,
                      rect: {x: 1, y: 2, w: 3, h: 4},
                    });
                  }, 25);
                }, args.waitMs);
              }, 25);
            }
            """, {"waitMs": 400})
        pg.evaluate(_ARM_CLOSE_LATER, {"delayMs": 2500})
        steps = record_session(pg, lambda _s: None, lambda _s: None)
    finally:
        pg.close()

    # 守卫生效（第一次 P 被忽略）→ 只收获第二次 P 的那一条
    assert len(steps) == 1
    assert steps[0]["kind"] == "download"
    assert steps[0]["selector"] == "#t"
    assert steps[0]["columns"] is None
    assert steps[0]["note"] == ""
