"""框选层测试：selector_from_chain 纯函数全分支 + run_pick/overlay 真浏览器集成。

内核缺失时如实 pytest.skip，不假绿（同 test_integration 风格）。
模拟"人确认"不用第二线程碰 playwright（sync API 禁跨线程）：在页面里
预埋轮询脚本，覆层注入后由页面自己调 __pageplay_onconfirm / 派发
Escape 键，等价于人在浏览器点击/按键。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay import picker  # noqa: E402
from pageplay.picker import (  # noqa: E402
    COLLECT_CHAIN_JS,
    PickCancelled,
    cover_screenshot,
    overlay_js,
    run_pick,
    selector_from_chain,
)

_PICK_HTML = """<html><body>
<h1>演示页</h1>
<table>
  <thead><tr><th>名称</th><th>价格</th></tr></thead>
  <tbody><tr><td>苹果</td><td>5.5</td></tr></tbody>
</table>
<a href="https://example.com/x.pdf">下载链接</a>
</body></html>"""

# 模拟人：覆层出现 → 锁定目标（与真实点击写入 __pageplay_locked 一致）→ 确认
_ARM_CONFIRM = """
(args) => {
  const timer = setInterval(() => {
    if (window.__pageplay_onconfirm
        && document.getElementById("__pageplay_overlay")) {
      clearInterval(timer);
      window.__pageplay_locked = document.querySelector(args.target);
      window.__pageplay_onconfirm(args.payload);
    }
  }, 25);
}
"""

# 模拟人按 Esc：覆层出现 → 对 document 派发 Escape 键（走真实 keydown 路径）
_ARM_ESCAPE = """
() => {
  const timer = setInterval(() => {
    if (document.getElementById("__pageplay_overlay")) {
      clearInterval(timer);
      document.dispatchEvent(
        new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    }
  }, 25);
}
"""


@pytest.fixture
def page():
    """无内核则跳过；有则起 headless 页面铺内联 HTML（表格 + 下载链接）。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过框选集成：{exc}")
    pg = browser.new_page()
    pg.set_content(_PICK_HTML)
    yield pg
    pg.close()
    browser.close()
    pw.stop()


# ----------------------------------------------------------------------
# selector_from_chain 纯函数全分支
# ----------------------------------------------------------------------

def test_selector_id_truncates_from_nearest_id():
    """链中任一环有 id → 从最靠近目标的 id 环起截断，前面环节丢弃。"""
    chain = [
        {"tag": "div", "id": "wrap", "classes": [], "nth": 1},
        {"tag": "ul", "id": None, "classes": ["list"], "nth": 1},
        {"tag": "li", "id": None, "classes": [], "nth": 3},
        {"tag": "a", "id": None, "classes": [], "nth": 1},
    ]
    assert selector_from_chain(chain) == "#wrap > ul.list > li:nth-of-type(3) > a"


def test_selector_id_on_target_alone_and_multiple_ids():
    """目标自带 id → 只剩 #id；多个 id → 取最靠近目标（最后）那个。"""
    assert selector_from_chain([
        {"tag": "div", "id": None, "classes": [], "nth": 1},
        {"tag": "a", "id": "x", "classes": [], "nth": 2},
    ]) == "#x"
    assert selector_from_chain([
        {"tag": "div", "id": "a1", "classes": [], "nth": 1},
        {"tag": "div", "id": None, "classes": [], "nth": 1},
        {"tag": "span", "id": "b2", "classes": [], "nth": 1},
    ]) == "#b2"


def test_selector_full_chain_nth_matches_contract_example():
    """无 id 全链拼接；首环省 nth；nth=1 冗余省略——逐字对齐契约示例。"""
    chain = [
        {"tag": "div", "id": None, "classes": ["main", "extra"], "nth": 1},
        {"tag": "ul", "id": None, "classes": ["list"], "nth": 1},
        {"tag": "li", "id": None, "classes": [], "nth": 3},
        {"tag": "a", "id": None, "classes": [], "nth": 1},
    ]
    assert selector_from_chain(chain) == "div.main > ul.list > li:nth-of-type(3) > a"


def test_selector_middle_link_nth_kept_when_gt_one():
    """中间环 nth>1 必须带上（同 tag 兄弟去重）；classes 只取第一个。"""
    chain = [
        {"tag": "div", "id": None, "classes": [], "nth": 1},
        {"tag": "p", "id": None, "classes": ["a", "b"], "nth": 2},
        {"tag": "span", "id": None, "classes": [], "nth": 1},
    ]
    out = selector_from_chain(chain)
    assert out == "div > p.a:nth-of-type(2) > span"  # 首环省 nth；中间环保留
    assert ".b" not in out  # classes 只取第一个


def test_selector_over_5_links_keeps_element_side():
    """链长上限 5 环，超出截头（保靠近元素侧）：离根最远的环被丢。"""
    chain = []
    for i in range(7):
        chain.append({"tag": "div", "id": None,
                      "classes": [f"drop{i}"] if i < 2 else [],
                      "nth": 1 if i < 2 else 4})
    out = selector_from_chain(chain)
    assert len(out.split(" > ")) == 5
    assert "drop0" not in out and "drop1" not in out  # 靠根两环已被截掉
    assert out == ("div > div:nth-of-type(4) > div:nth-of-type(4)"
                   " > div:nth-of-type(4) > div:nth-of-type(4)")


def test_selector_empty_chain_raises():
    with pytest.raises(ValueError):
        selector_from_chain([])


# ----------------------------------------------------------------------
# COLLECT_CHAIN_JS / run_pick / overlay 集成（真 chromium）
# ----------------------------------------------------------------------

def test_collect_chain_js_shape(page):
    """collect 脚本可独立复用：根在首、目标在尾，字段齐。"""
    page.evaluate("() => { window.__pageplay_locked = document.querySelector('table'); }")
    chain = page.evaluate(COLLECT_CHAIN_JS)
    assert [c["tag"] for c in chain] == ["html", "body", "table"]
    assert all(set(c) >= {"tag", "id", "classes", "nth"} for c in chain)
    assert chain[-1]["nth"] == 1 and chain[0]["id"] is None


def test_run_pick_table_confirm(page):
    """table 档：confirm payload 原样透传（columns/rect），selector 命中该表。"""
    payload = {
        "selector_hint": "table",
        "action": "table",
        "columns": ["名称", "价格"],
        "rect": {"x": 8, "y": 40, "w": 200, "h": 60},
    }
    page.evaluate(_ARM_CONFIRM, {"target": "table", "payload": payload})
    calls: list[dict] = []
    result = run_pick(page, calls.append)

    assert calls == [result]  # on_confirm 在返回前被调，收到组装结果
    assert result["action"] == "table"
    assert result["columns"] == ["名称", "价格"]  # 原样，不与页面表头求交
    assert result["rect"] == payload["rect"]
    assert page.query_selector(result["selector"]) is not None
    assert page.query_selector(result["selector"]).evaluate(
        "el => el.tagName.toLowerCase()") == "table"
    # 结束后覆层已移除，页面业务 DOM 不留痕
    assert page.evaluate("() => !document.getElementById('__pageplay_overlay')")


def test_run_pick_download_action(page):
    """download 档：action/selector 正确，selector 命中该 a 链接。"""
    payload = {"selector_hint": "a", "action": "download",
               "columns": None, "rect": {"x": 1, "y": 2, "w": 3, "h": 4}}
    page.evaluate(_ARM_CONFIRM, {"target": "a", "payload": payload})
    result = run_pick(page, lambda _r: None)

    assert result["action"] == "download"
    assert result["columns"] is None
    assert page.query_selector(result["selector"]) is not None
    assert page.query_selector(result["selector"]).get_attribute("href") == \
        "https://example.com/x.pdf"


def test_run_pick_escape_raises_cancelled(page):
    """Esc 路径：覆层暴露的 keydown 处理收到 Escape → PickCancelled。"""
    page.evaluate(_ARM_ESCAPE)
    with pytest.raises(PickCancelled):
        run_pick(page, lambda _r: None)
    # 取消同样清场
    assert page.evaluate("() => !document.getElementById('__pageplay_overlay')")


def test_overlay_js_reinjection_is_single_instance(page):
    """重复注入先清旧实例：页面只剩一个覆层。"""
    page.evaluate(overlay_js())
    page.evaluate(overlay_js())
    assert page.evaluate(
        "() => document.querySelectorAll('#__pageplay_overlay').length") == 1
    page.evaluate("() => window.__pageplay_cleanup && window.__pageplay_cleanup()")
    assert page.evaluate("() => !document.getElementById('__pageplay_overlay')")


# ----------------------------------------------------------------------
# cover_screenshot
# ----------------------------------------------------------------------

def test_cover_screenshot_writes_png(page, tmp_path: Path):
    out = tmp_path / "cover.png"
    assert cover_screenshot(page, "table", out) == out
    assert out.is_file() and out.stat().st_size > 0


def test_cover_screenshot_missing_selector_raises(page, tmp_path: Path):
    with pytest.raises(ValueError) as ei:
        cover_screenshot(page, "table.missing", tmp_path / "x.png")
    assert "重新框选" in str(ei.value)


# ----------------------------------------------------------------------
# T8：框选层跨页存活（init script 跨文档布防 + 心跳自愈 + url 记当下）
# ----------------------------------------------------------------------

_T8_TIMEOUT = 20  # 测试专用兜底超时：自愈/跨页失败快速暴露，不等 600s

# 起点页替人走真实跨页链路（T7c 点击语义：未锁定时第一击=截获锁定，
# 锁定态下点击放行）：先见到覆层 → 第一击锁 h1（等价真人先框选过，
# 面板在场）→ 第二击点 #go 放行 → 真导航；覆层在场用 sessionStorage 记见证
_ARM_CLICK_GO = """
() => {
  const t = setInterval(() => {
    if (!document.getElementById("__pageplay_overlay")) return;
    if (!window.__pageplay_t8_locked) {
      window.__pageplay_t8_locked = true;
      document.querySelector("h1").click();
      return;
    }
    clearInterval(t);
    sessionStorage.setItem("pageplay-t8-start-overlay", "1");
    document.getElementById("go").click();
  }, 25);
}
"""

# 新文档替人确认（add_init_script 随 /table 文档执行）：见到覆层与表格
# 都在场才锁定 #data 调确认绑定——覆层不在场即说明 init script 没生效。
# 注意 add_init_script 传字符串只按脚本源码解析、不自动调用，必须 IIFE。
_ARM_CONFIRM_ON_TABLE = """
(() => {
  if (location.pathname !== "/table") return;
  const t = setInterval(() => {
    if (window.__pageplay_onconfirm
        && document.getElementById("__pageplay_overlay")
        && document.getElementById("data")) {
      clearInterval(t);
      sessionStorage.setItem("pageplay-t8-table-overlay", "1");
      window.__pageplay_locked = document.getElementById("data");
      window.__pageplay_onconfirm({
        selector_hint: "#data", action: "table",
        columns: ["名称", "价格", "库存", "链接"],
        rect: {x: 8, y: 40, w: 200, h: 60},
      });
    }
  }, 25);
})();
"""

# SPA 单页：按钮软跳转（pushState 换路径 + innerHTML 重建业务 DOM）
_SPA_HTML = """<html><body>
<div id="app"><h1>SPA 首屏</h1><button id="nav">换页</button></div>
<script>
  document.getElementById("nav").addEventListener("click", () => {
    history.pushState({}, "", "/list");
    document.getElementById("app").innerHTML =
      "<table id='data'><thead><tr><th>名称</th></tr></thead>"
      + "<tbody><tr><td>苹果</td></tr></tbody></table>";
  });
</script>
</body></html>"""

# 软跳转替人：见覆层 → 触发软跳转（业务 DOM 连同覆层被剥掉）→ 心跳
# 自愈覆层重新在场后，锁定新 DOM 的表格确认
_ARM_SOFT_NAV = """
() => {
  const t = setInterval(() => {
    if (!document.getElementById("__pageplay_overlay")) return;
    clearInterval(t);
    history.pushState({}, "", "/list");
    document.getElementById("app").innerHTML =
      "<table id='data'><thead><tr><th>名称</th></tr></thead>"
      + "<tbody><tr><td>苹果</td></tr></tbody></table>";
    const t2 = setInterval(() => {
      if (window.__pageplay_onconfirm
          && document.getElementById("__pageplay_overlay")
          && document.getElementById("data")) {
        clearInterval(t2);
        window.__pageplay_locked = document.getElementById("data");
        window.__pageplay_onconfirm({
          selector_hint: "#data", action: "table", columns: null,
          rect: {x: 8, y: 40, w: 200, h: 60},
        });
      }
    }, 50);
  }, 25);
}
"""


def _launch_chromium():
    """起 headless chromium，返回 (pw, browser)；内核缺失返回 (None, None)。"""
    try:
        pw = sync_playwright().start()
        return pw, pw.chromium.launch(headless=True)
    except Exception:  # 内核缺失/驱动起不来：调用方如实跳过，不假绿
        return None, None


def test_overlay_survives_full_navigation(nav_site, monkeypatch):
    """真导航跨页：新文档由 init script 自动重新布防，确认仍达 python。

    run_pick 阻塞期间 sync API 单线程，python 无法在场直接断言，用
    sessionStorage（同源跨导航存活）作覆层在场见证：起点页脚本先见到
    覆层才点链接；/table 的 init 脚本先见到覆层才确认——任何一步覆层
    缺席都会卡到兜底超时，测试必失败。
    """
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _T8_TIMEOUT)
    pw, browser = _launch_chromium()
    if browser is None:
        pytest.skip("playwright chromium 不可用，跳过跨页存活")
    pg = browser.new_page()
    try:
        pg.goto(nav_site + "/start")
        pg.add_init_script(_ARM_CONFIRM_ON_TABLE)  # 新文档替人确认
        pg.evaluate(_ARM_CLICK_GO)                 # 当前文档替人点链接
        result = run_pick(pg, lambda _r: None)

        assert result["action"] == "table"
        assert result["columns"] == ["名称", "价格", "库存", "链接"]
        assert pg.url.startswith(nav_site + "/table")
        assert result["url"] == pg.url  # 确认那一刻的落点：/table，非 /start
        # 两个文档里覆层都真实在场过（sessionStorage 见证）
        assert pg.evaluate(
            "() => sessionStorage.getItem('pageplay-t8-start-overlay')") == "1"
        assert pg.evaluate(
            "() => sessionStorage.getItem('pageplay-t8-table-overlay')") == "1"
        # 结束后当前文档覆层已清场，业务 DOM 不留痕
        assert pg.evaluate(
            "() => !document.getElementById('__pageplay_overlay')")
    finally:
        pg.close()
        browser.close()
        pw.stop()


def test_overlay_survives_soft_navigation(page, nav_site, monkeypatch):
    """SPA 软跳转：同文档 DOM 重建剥掉覆层 → 心跳自愈重新布防可继续框选。

    先 goto 假站再 set_content（保住真 origin；about:blank 上 pushState
    换路径会被浏览器拒绝），pushState 换路径 + innerHTML 重建即典型 SPA
    软跳转，覆层随 body 重建被剥，等心跳自愈后替人确认。
    """
    monkeypatch.setattr(picker, "_PICK_TIMEOUT_SEC", _T8_TIMEOUT)
    page.goto(nav_site + "/start")
    page.set_content(_SPA_HTML)
    page.evaluate(_ARM_SOFT_NAV)
    result = run_pick(page, lambda _r: None)

    assert result["action"] == "table"
    assert result["url"].endswith("/list")  # 确认在软跳转后的路径上发生
    assert page.query_selector(result["selector"]) is not None  # 新 DOM 命中
    # 结束后覆层已清场（心跳同停），业务 DOM 不留痕
    assert page.evaluate("() => !document.getElementById('__pageplay_overlay')")
