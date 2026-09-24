"""T9a 会话常驻测试：run_pick(repeat=True) 确认后继续框选，人关窗才结束。

独立成文件的原因：test_picker.py 已 388 行，追加会破单文件 500 行红线
（#26），且 test_picker.py 逐字零改动 = repeat=False 回归的最强保证。
模拟"人"沿用本目录惯例：不用第二线程碰 playwright（sync API 禁跨
线程），在页面里预埋轮询脚本，由页面自己调 __pageplay_onconfirm /
window.close()，等价于真人点击确认 / 关窗。
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay import picker  # noqa: E402
from pageplay.picker import PickCancelled, run_pick  # noqa: E402

_PICK_HTML = """<html><body>
<h1>演示页</h1>
<table>
  <thead><tr><th>名称</th><th>价格</th></tr></thead>
  <tbody><tr><td>苹果</td><td>5.5</td></tr></tbody>
</table>
<a href="https://example.com/x.pdf">下载链接</a>
</body></html>"""

_REPEAT_TIMEOUT = 10  # 兜底超时：退出路径卡死快速暴露，不等 600s

# 替人连续确认：队列逐条发；"见到全新覆层节点" = python 已清场重布防、
# 可发下一条（每轮确认后旧实例死亡，重注的覆层是全新 DOM 节点）。
# step.delayMs = 见到新覆层后再等这么多毫秒才确认（测 deadline 刷新用）。
_ARM_CONFIRM_QUEUE = """
(args) => {
  let i = 0, lastOverlay = null;
  const timer = setInterval(() => {
    const ov = document.getElementById("__pageplay_overlay");
    if (!window.__pageplay_onconfirm || !ov || ov === lastOverlay) return;
    if (i >= args.queue.length) { clearInterval(timer); return; }
    const step = args.queue[i++];
    lastOverlay = ov;
    setTimeout(() => {
      window.__pageplay_locked = document.querySelector(step.target);
      window.__pageplay_onconfirm(step.payload);
    }, step.delayMs || 0);
  }, 25);
}
"""

# 替人延时关窗：headless 下脚本可关 CDP 页（实测 wait_for_timeout 抛
# TargetClosedError），与真人关窗走同一条 Page closed 退出路径。
_ARM_CLOSE_LATER = """
(args) => { setTimeout(() => window.close(), args.delayMs); }
"""

_QUEUE3 = [
    {"target": "table", "payload": {"selector_hint": "table", "action": "table",
                                    "columns": ["名称", "价格"],
                                    "rect": {"x": 8, "y": 40, "w": 200, "h": 60}}},
    {"target": "a", "payload": {"selector_hint": "a", "action": "download",
                                "columns": None,
                                "rect": {"x": 1, "y": 2, "w": 3, "h": 4}}},
    {"target": "h1", "payload": {"selector_hint": "h1", "action": "download",
                                 "columns": None,
                                 "rect": {"x": 5, "y": 6, "w": 7, "h": 8}}},
]
# 无 id/无 class 平铺 DOM，链即 html > body > <target>，逐字可断言
_EXPECTED_SELECTORS = ["html > body > table", "html > body > a", "html > body > h1"]


@pytest.fixture
def page():
    """无内核则跳过；有则起 headless 页面铺内联 HTML（同 test_picker）。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过会话常驻测试：{exc}")
    pg = browser.new_page()
    pg.set_content(_PICK_HTML)
    yield pg
    pg.close()
    browser.close()
    pw.stop()


def test_repeat_three_confirms(page, monkeypatch):
    """三连确认（不同 action/目标）→ 3 元素列表、顺序对、payload 完整。"""
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _REPEAT_TIMEOUT)
    page.evaluate(_ARM_CONFIRM_QUEUE, {"queue": _QUEUE3})
    page.evaluate(_ARM_CLOSE_LATER, {"delayMs": 1500})  # 三连后人关窗收摊
    calls: list[dict] = []
    results = run_pick(page, calls.append, repeat=True)

    assert isinstance(results, list) and len(results) == 3
    assert calls == results  # 每条确认都按序回调 on_confirm
    assert [r["action"] for r in results] == ["table", "download", "download"]
    assert results[0]["columns"] == ["名称", "价格"]  # payload 原样透传
    assert results[1]["rect"] == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert [r["selector"] for r in results] == _EXPECTED_SELECTORS
    assert results[0]["url"]  # 确认时刻落点 url 有值


def test_repeat_close_after_one_confirm(page, monkeypatch):
    """单条确认后关窗 → 正常返回 1 条列表，不抛。"""
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _REPEAT_TIMEOUT)
    page.evaluate(_ARM_CONFIRM_QUEUE, {"queue": _QUEUE3[:1]})
    page.evaluate(_ARM_CLOSE_LATER, {"delayMs": 700})
    results = run_pick(page, lambda _r: None, repeat=True)

    assert isinstance(results, list) and len(results) == 1
    assert results[0]["action"] == "table"
    assert results[0]["selector"] == _EXPECTED_SELECTORS[0]


def test_repeat_close_before_any_confirm(page, monkeypatch):
    """零确认关窗 → PickCancelled（走既有 Page closed 退出路径）。"""
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _REPEAT_TIMEOUT)
    page.evaluate(_ARM_CLOSE_LATER, {"delayMs": 400})
    with pytest.raises(PickCancelled, match="页面已关闭"):
        run_pick(page, lambda _r: None, repeat=True)


def test_repeat_idle_timeout_returns_collected(page, monkeypatch, caplog):
    """空闲超时：已收 1 条后无交互 → 不抛，返回已收列表并 log 说明。"""
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", 1)
    page.evaluate(_ARM_CONFIRM_QUEUE, {"queue": _QUEUE3[:1]})
    caplog.set_level(logging.INFO)
    results = run_pick(page, lambda _r: None, repeat=True)  # 之后无人再交互

    assert isinstance(results, list) and len(results) == 1
    assert "空闲超时" in caplog.text  # 收摊原因如实落日志
    # 会话结束覆层已清场（超时退出同样走 finally），业务 DOM 不留痕
    assert page.evaluate("() => !document.getElementById('__pageplay_overlay')")


def test_repeat_deadline_refreshes_after_each_confirm(page, monkeypatch):
    """deadline 每轮确认后刷新：两轮间隔超过原 deadline 仍能继续收。

    反证：若 deadline 不刷新，第二条确认（距首轮 700ms、总时限 1s）
    到来前会话已超时收摊，只能返回 1 条。
    """
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", 1)
    queue = [dict(_QUEUE3[0]), dict(_QUEUE3[1], delayMs=700)]
    page.evaluate(_ARM_CONFIRM_QUEUE, {"queue": queue})
    page.evaluate(_ARM_CLOSE_LATER, {"delayMs": 1600})
    results = run_pick(page, lambda _r: None, repeat=True)

    assert isinstance(results, list) and len(results) == 2
