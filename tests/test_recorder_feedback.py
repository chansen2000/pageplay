"""T16 录制页面反馈测试：REC 角标 / hover 轻高亮 / 框选弹条 / cleanup。

JS 行为用 evaluate(recorder_js()) 注入后 python 侧直接断言 DOM（真
chromium，不起会话）；「角标按钮 = 等价按 P」走 record_session 端到端
（预埋脚本替人：点角标按钮 → 覆层在场时 sessionStorage 存弹条/角标显隐
见证 → 确认 → 回浏览后再见证切回——心跳 500ms，见证延时 700ms 留足），
同 test_recorder 惯例：不用第二线程碰 playwright，替人编排全在页面里。
内核缺失如实 pytest.skip，不假绿。flow_site 夹具见 conftest。
"""

from __future__ import annotations

import json

import pytest

from pageplay import picker, recorder
from pageplay.recorder import record_session

from test_recorder import rec_browser  # noqa: F401  复用真 chromium 夹具

_IDLE_TIMEOUT = 4   # 端到端用例收摊时限（兼编排卡死快速暴露）
_PICK_TIMEOUT = 8   # 框选兜底：编排卡死快速暴露，不等 600s

_JS_STATE = """
() => ({
  badge: (() => { const b = document.getElementById("__pageplay_rec_badge");
                  return b ? {display: b.style.display,
                              text: b.textContent,
                              pe: getComputedStyle(b).pointerEvents,
                              btns: Array.prototype.map.call(
                                  b.querySelectorAll("button"),
                                  x => ({text: x.textContent,
                                         pe: getComputedStyle(x).pointerEvents}))}
                  : null; })(),
  banner: (() => { const b = document.getElementById("__pageplay_rec_banner");
                   return b ? {display: b.style.display, text: b.textContent}
                            : null; })(),
})
"""


def test_badge_present_button_clickable_banner_hidden(rec_browser, flow_site):
    """布防即有 REC 角标（● 录制中 + 框选按钮）；角标不挡操作、弹条藏。"""
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.evaluate(recorder.recorder_js())
        st = pg.evaluate(_JS_STATE)
        assert st["badge"] is not None
        assert "●" in st["badge"]["text"] and "录制中" in st["badge"]["text"]
        assert [b["text"] for b in st["badge"]["btns"]] == ["框选"]
        assert st["badge"]["pe"] == "none"           # 角标本体不挡操作
        assert st["badge"]["btns"][0]["pe"] == "auto"  # 只有按钮可点
        assert st["badge"]["display"] == "flex"      # 浏览态：角标在
        assert st["banner"]["display"] == "none"     # 浏览态：弹条藏
        assert "框选模式" in st["banner"]["text"]
    finally:
        pg.close()


def test_badge_button_click_equals_p_key_harvests(rec_browser, flow_site,
                                                  monkeypatch):
    """端到端：点角标「框选」按钮与按 P 等价——进 picker 子模式、弹条在场、
    确认后收获步落账、回浏览后弹条藏角标回（心跳切换）。"""
    monkeypatch.setattr(recorder, "_RECORD_IDLE_TIMEOUT_SEC", _IDLE_TIMEOUT)
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _PICK_TIMEOUT)
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.evaluate(_ARM_BADGE_PICK)
        steps = record_session(pg, lambda _s: None, lambda _s: None)

        # 收获步与按 P 的形状一致（等价性的账面证据）
        assert [s["kind"] for s in steps] == ["table"]
        assert steps[0]["selector"] == "#t1"
        assert steps[0]["columns"] == ["A"]
        # 子模式在场见证：弹条显示、角标隐藏
        in_pick = json.loads(pg.evaluate(
            "() => sessionStorage.getItem('pageplay-t16-in-pick')"))
        assert in_pick == {
            "banner": "block",
            "badge": "none",
            "text": "框选模式：红框罩住目标后单击锁定（Esc 返回浏览）",
        }
        # 回浏览见证（700ms > 500ms 心跳）：角标回、弹条藏
        resumed = json.loads(pg.evaluate(
            "() => sessionStorage.getItem('pageplay-t16-resumed')"))
        assert resumed == {"banner": "none", "badge": "flex"}
    finally:
        pg.close()


def test_hover_outline_on_move_and_input_exempt(rec_browser, flow_site):
    """hover 出 1px 虚线 outline，移开消失；INPUT 豁免；弹条浏览态不闪出。"""
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.evaluate(recorder.recorder_js())
        pg.evaluate(
            "() => { const i = document.createElement('input'); i.id = 'q';"
            " document.body.appendChild(i); }")
        pg.hover("#to-list")
        assert "dashed" in pg.evaluate(
            "() => document.getElementById('to-list').style.outline")
        pg.hover("h1")  # 移到别的元素：旧高亮移除，新目标高亮
        assert pg.evaluate(
            "() => document.getElementById('to-list').style.outline") == ""
        assert "dashed" in pg.evaluate(
            "() => document.querySelector('h1').style.outline")
        pg.hover("#q")  # INPUT 豁免：不加高亮
        assert pg.evaluate(
            "() => document.getElementById('q').style.outline") == ""
        assert pg.evaluate(_JS_STATE)["banner"]["display"] == "none"
    finally:
        pg.close()


def test_pick_banner_shows_on_p_and_hides_after_resume(rec_browser, flow_site):
    """P 进框选：弹条即时出现（不等心跳）；python 清标记回浏览后心跳切回。"""
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.evaluate(recorder.recorder_js())
        pg.evaluate("""() => document.dispatchEvent(new KeyboardEvent(
            "keydown", {key: "p", bubbles: true}))""")
        st = pg.evaluate(_JS_STATE)
        assert st["banner"]["display"] == "block"
        assert st["banner"]["text"] == \
            "框选模式：红框罩住目标后单击锁定（Esc 返回浏览）"
        assert st["badge"]["display"] == "none"
        assert pg.evaluate("() => window.__pageplay_picking") is True
        # python 侧回浏览语义 = 清 picking 标记（recorder._resume_browse 同款）
        pg.evaluate("() => { window.__pageplay_picking = false; }")
        pg.wait_for_timeout(700)
        st = pg.evaluate(_JS_STATE)
        assert st["banner"]["display"] == "none"
        assert st["badge"]["display"] == "flex"
    finally:
        pg.close()


def test_cleanup_removes_badge_banner_and_hover(rec_browser, flow_site):
    """cleanup（会话收尾路径）：角标/弹条移除、hover 监听拆掉、高亮还原。"""
    pg = rec_browser.new_page()
    try:
        pg.goto(flow_site + "/start")
        pg.evaluate(recorder.recorder_js())
        pg.hover("#to-list")
        assert "dashed" in pg.evaluate(
            "() => document.getElementById('to-list').style.outline")
        pg.evaluate("() => window.__pageplay_rec_cleanup()")
        st = pg.evaluate(_JS_STATE)
        assert st["badge"] is None and st["banner"] is None
        assert pg.evaluate(
            "() => document.getElementById('to-list').style.outline") == ""
        pg.hover("h1")  # 监听已拆：不再加高亮
        assert pg.evaluate(
            "() => document.querySelector('h1').style.outline") == ""
    finally:
        pg.close()


# 替人（端到端用例）：等布防 → 点角标「框选」按钮 → 覆层在场时 sessionStorage
# 存子模式见证（弹条显示/角标隐藏）→ 锁定 #t1 确认（table 一列）→ 等 python
# 清 picking 回浏览 → 700ms（>500ms 心跳）后存回浏览见证。录制会话由空闲
# 超时收摊（flow_site 是 goto 页，脚本关窗无效，见 test_recorder 模块说明）。
_ARM_BADGE_PICK = """
() => {
  const t = setInterval(() => {
    if (!(window.__pageplay_step && window.__pageplay_picking === false)) return;
    clearInterval(t);
    setTimeout(() => {
      const btn = document.querySelector("#__pageplay_rec_badge button");
      if (!btn) { sessionStorage.setItem("pageplay-t16-no-badge", "1"); return; }
      btn.click();
      const t2 = setInterval(() => {
        if (!document.getElementById("__pageplay_overlay")) return;
        clearInterval(t2);
        sessionStorage.setItem("pageplay-t16-in-pick", JSON.stringify({
          banner: document.getElementById("__pageplay_rec_banner").style.display,
          badge: document.getElementById("__pageplay_rec_badge").style.display,
          text: document.getElementById("__pageplay_rec_banner").textContent,
        }));
        window.__pageplay_locked = document.getElementById("t1");
        window.__pageplay_onconfirm({
          selector_hint: "#t1", action: "table", columns: ["A"],
          rect: {x: 1, y: 2, w: 3, h: 4},
        });
        const t3 = setInterval(() => {
          if (window.__pageplay_picking) return;  // 等 python 清标记回浏览
          clearInterval(t3);
          setTimeout(() => {
            sessionStorage.setItem("pageplay-t16-resumed", JSON.stringify({
              banner: document.getElementById("__pageplay_rec_banner")
                .style.display,
              badge: document.getElementById("__pageplay_rec_badge")
                .style.display,
            }));
          }, 700);
        }, 25);
      }, 25);
    }, 300);
  }, 25);
}
"""
