"""v0.7-A 卡片列表识别与提取测试：hover 组锁定/字段勾选/runner 集成/flows 校验。

内核缺失时如实 pytest.skip，不假绿（同 test_picker 风格）。模拟"人"
不用第二线程碰 playwright（sync API 禁跨线程）：页面内预埋轮询脚本，
覆层出现后向卡片内叶子派发合成 mousemove/click——走真实 hover 优先级
→ 组锁定 → renderFieldBar → 确认链路，不绕过任何 JS 逻辑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay import runner  # noqa: E402
from pageplay.flows import load_flow, save_flow  # noqa: E402
from pageplay.picker import run_pick  # noqa: E402

# 首卡前两个字段的期望（与 conftest _CARDS_HTML 逐字对应；label=前 8 字，
# rel 链 item 端在首——与 COLLECT_CHAIN_JS"根在首"同向）
_EXPECT_FIELDS = [
    {"label": "TB9001", "rel": [{"tag": "div", "nth": 1}]},
    {"label": "无线蓝牙鼠标静音", "rel": [{"tag": "div", "nth": 2},
                                          {"tag": "span", "nth": 1}]},
]

# 替人框卡片：覆层在场 → 合成 mousemove/click 到首卡标题（真实 hover 链路
# 命中重复兄弟组、高亮父容器）→ 记下面板文案与字段数 → 只留前 keep 个勾选
# → 点确认。任何一步覆层/绑定缺席都会卡到兜底超时，测试必失败。
_ARM_CARDS = """
(args) => {
  const timer = setInterval(() => {
    if (!window.__pageplay_onconfirm
        || !document.getElementById("__pageplay_overlay")) return;
    clearInterval(timer);
    const span = document.querySelector(".card .title");
    span.dispatchEvent(new MouseEvent("mousemove", {bubbles: true}));
    span.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    const panel = document.getElementById("__pageplay_overlay").children[1];
    window.__pp_panel_text = panel.textContent;
    const boxes = panel.querySelectorAll("input[type=checkbox]");
    window.__pp_field_count = boxes.length;
    boxes.forEach((cb, i) => { if (i >= args.keep) cb.checked = false; });
    const ok = Array.prototype.find.call(
      panel.querySelectorAll("button"), b => b.textContent === "确认");
    ok.click();
  }, 25);
}
"""

# 替人框表格 td：语义容器必须优先于卡片组（4 个 td 同签名也成组，但表格
# 在场时 hover 应落在 table 上、面板是列勾选条）——优先级契约的反面_guard
_ARM_TABLE_TD = """
() => {
  const timer = setInterval(() => {
    if (!window.__pageplay_onconfirm
        || !document.getElementById("__pageplay_overlay")) return;
    clearInterval(timer);
    const td = document.querySelector("td");
    td.dispatchEvent(new MouseEvent("mousemove", {bubbles: true}));
    td.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    const panel = document.getElementById("__pageplay_overlay").children[1];
    window.__pp_panel_text = panel.textContent;
    const ok = Array.prototype.find.call(
      panel.querySelectorAll("button"), b => b.textContent === "确认");
    ok.click();
  }, 25);
}
"""


@pytest.fixture
def pick_page():
    """真 chromium headless 空白页（导航由用例自己定）；内核缺失如实 skip。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过卡片拾取集成：{exc}")
    pg = browser.new_page()
    yield pg
    pg.close()
    browser.close()
    pw.stop()


@pytest.fixture
def cards_browser():
    """真 chromium headless 浏览器 + 预建上下文（run_flow 消费 contexts[0]）；
    内核缺失如实 skip。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"playwright chromium 不可用，跳过 runner 卡片链：{exc}")
    browser.new_context()  # run_flow 取 contexts[0] 开页，须先有上下文
    yield browser
    browser.close()
    pw.stop()


# ----------------------------------------------------------------------
# picker：hover 识别卡片组 → 字段勾选 → 确认 payload（真 chromium）
# ----------------------------------------------------------------------

def test_cards_hover_lock_payload(pick_page, card_site):
    """hover 首卡叶子 → 整组锁定（父容器），面板提示 N 条记录；确认出
    list_mode=cards + 已勾选 fields 原样透传，selector 命中父容器。"""
    pick_page.goto(f"{card_site}/cards")
    pick_page.evaluate(_ARM_CARDS, {"keep": 2})
    calls: list[dict] = []
    result = run_pick(pick_page, calls.append)

    assert calls == [result]  # on_confirm 在返回前被调
    assert result["action"] == "table"
    assert result["list_mode"] == "cards"
    assert result["columns"] is None
    assert result["fields"] == _EXPECT_FIELDS  # 原样（label 截 8 字 + rel 链）
    # 面板真实出现过"识别到 6 条记录"，字段候选共 4 个（订单号/标题/价格/状态）
    assert "识别到 6 条记录" in pick_page.evaluate("() => window.__pp_panel_text")
    assert pick_page.evaluate("() => window.__pp_field_count") == 4
    # selector 指向组父容器（.list），页面可命中
    el = pick_page.query_selector(result["selector"])
    assert el is not None
    assert el.evaluate("e => e.className") == "list"
    assert result["url"].endswith("/cards")
    # 结束后覆层已移除，页面业务 DOM 不留痕
    assert pick_page.evaluate(
        "() => !document.getElementById('__pageplay_overlay')")


def test_hover_prefers_semantic_table_over_card_group(pick_page, table_site):
    """优先级契约：语义容器 > 重复兄弟组——td 们虽同签名成组，hover 仍落
    table、面板是列勾选条，payload 为 list_mode=table（表格路径原样）。"""
    pick_page.goto(f"{table_site}/table")
    pick_page.evaluate(_ARM_TABLE_TD)
    result = run_pick(pick_page, lambda _r: None)

    assert "已锁定表格" in pick_page.evaluate("() => window.__pp_panel_text")
    assert result["list_mode"] == "table"
    assert result["fields"] is None
    assert result["columns"] == ["名称", "价格", "库存", "链接"]


# ----------------------------------------------------------------------
# T21b 误报收紧：文本全是 style/script 的垃圾组 → 两道闸拦下
# ----------------------------------------------------------------------

# 真机淘宝首页缺陷形态：3 个同签名兄弟，"文本"实为 <style> 里的 CSS
# （.feedsCa.skeleton…）——textContent 充数成卡片文本，旧逻辑给出巨区
# 红框 + "识别到 3 条记录" + 垃圾字段勾选条。
_TRASH_HTML = """<html><body>
<h1>搜索框区域</h1>
<div class="feeds-wrap">
  <div class="feedsCa"><style>.feedsCa.skeleton{width:100px;height:80px}</style></div>
  <div class="feedsCa"><style>.feedsCa.item{background:#f4f4f4;margin:8px}</style></div>
  <div class="feedsCa"><style>.feedsCa.end{display:none;opacity:0}</style></div>
</div>
</body></html>"""

# 替人框垃圾组首项：锁定组父容器 → 记面板文案/勾选数/有无确认与下载按钮
# → 点"下载此元素"（闸2 下唯一出口），确认 payload 走 download 通道。
_ARM_TRASH_GROUP = """
() => {
  const timer = setInterval(() => {
    if (!window.__pageplay_onconfirm
        || !document.getElementById("__pageplay_overlay")) return;
    clearInterval(timer);
    const item = document.querySelector(".feedsCa");
    item.dispatchEvent(new MouseEvent("mousemove", {bubbles: true}));
    item.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    const panel = document.getElementById("__pageplay_overlay").children[1];
    window.__pp_panel_text = panel.textContent;
    window.__pp_field_count = panel.querySelectorAll("input[type=checkbox]").length;
    window.__pp_has_ok = Array.prototype.some.call(
      panel.querySelectorAll("button"), b => b.textContent === "确认");
    const dl = Array.prototype.find.call(
      panel.querySelectorAll("button"), b => b.textContent === "下载此元素");
    window.__pp_has_dl = !!dl;
    if (dl) dl.click();
  }, 25);
}
"""


def test_trash_style_only_group_gated_to_download(pick_page):
    """T21b 两道闸：同签名兄弟的文本宿主全是 <style>（CSS 充数）→ 闸1
    过滤后字段 <2 → 闸2 不渲染勾选条：灰字"未识别到有效的重复数据结构"
    + 只留"下载此元素"（无勾选框/无确认，垃圾组没有可确认的字段路径）；
    确认出 action=download、fields=null（list_mode 走缺省 table 非 cards）。"""
    pick_page.set_content(_TRASH_HTML)
    pick_page.evaluate(_ARM_TRASH_GROUP)
    result = run_pick(pick_page, lambda _r: None)

    panel_text = pick_page.evaluate("() => window.__pp_panel_text")
    assert "未识别到有效的重复数据结构" in panel_text
    assert "识别到 3 条记录" not in panel_text  # 勾选条标题不出现（含字段选择入口）
    assert pick_page.evaluate("() => window.__pp_field_count") == 0
    assert pick_page.evaluate("() => window.__pp_has_ok") is False
    assert pick_page.evaluate("() => window.__pp_has_dl") is True
    assert result["action"] == "download"
    assert result["list_mode"] == "table"
    assert result["fields"] is None


# ----------------------------------------------------------------------
# T21c hover 标签不误导：垃圾组 hover 不冒充"卡片列表"，好组照旧
# ----------------------------------------------------------------------

# 替人 hover 垃圾组首项：只 mousemove 记 hover 红框标签 → 再点击锁定记
# 锁定态标签 → 闸2 下唯一出口"下载此元素"收场（run_pick 需一次确认才返回）。
_ARM_TRASH_HOVER = """
() => {
  const timer = setInterval(() => {
    if (!window.__pageplay_onconfirm
        || !document.getElementById("__pageplay_overlay")) return;
    clearInterval(timer);
    const item = document.querySelector(".feedsCa");
    item.dispatchEvent(new MouseEvent("mousemove", {bubbles: true}));
    window.__pp_hover_label =
      document.getElementById("__pageplay_boxlabel").textContent;
    item.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    window.__pp_lock_label =
      document.getElementById("__pageplay_boxlabel").textContent;
    const dl = Array.prototype.find.call(
      document.getElementById("__pageplay_overlay").children[1]
        .querySelectorAll("button"), b => b.textContent === "下载此元素");
    dl.click();
  }, 25);
}
"""

# 替人 hover 正常卡片组首卡叶子：记 hover 标签 → 点击锁定 → 字段全勾确认
# 收场（质量判定前移后，好组文案必须原样保留）。
_ARM_CARDS_HOVER = """
() => {
  const timer = setInterval(() => {
    if (!window.__pageplay_onconfirm
        || !document.getElementById("__pageplay_overlay")) return;
    clearInterval(timer);
    const span = document.querySelector(".card .title");
    span.dispatchEvent(new MouseEvent("mousemove", {bubbles: true}));
    window.__pp_hover_label =
      document.getElementById("__pageplay_boxlabel").textContent;
    span.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    const ok = Array.prototype.find.call(
      document.getElementById("__pageplay_overlay").children[1]
        .querySelectorAll("button"), b => b.textContent === "确认");
    ok.click();
  }, 25);
}
"""


def test_trash_group_hover_label_not_card_list(pick_page):
    """T21c：垃圾组（文本全是 <style> CSS）hover 标签=「区域：未识别到
    列表结构」，不含"卡片列表"字样，锁定态标签与之一致——hover 阶段就
    按闸2 同口径判质量，不再出现"hover 报卡片列表、点击却未识别到结构"
    的误导（真机淘宝首页搜索框区缺陷）。"""
    pick_page.set_content(_TRASH_HTML)
    pick_page.evaluate(_ARM_TRASH_HOVER)
    run_pick(pick_page, lambda _r: None)

    hover = pick_page.evaluate("() => window.__pp_hover_label")
    assert "未识别到列表结构" in hover
    assert "卡片列表" not in hover
    assert pick_page.evaluate("() => window.__pp_lock_label") == hover


def test_card_group_hover_label_still_card_list(pick_page, card_site):
    """正常卡片组 hover 标签照旧「卡片列表：6 条记录」——质量判定前移
    不改好组既有文案（质量闸只拦字段<2 的垃圾组）。"""
    pick_page.goto(f"{card_site}/cards")
    pick_page.evaluate(_ARM_CARDS_HOVER)
    run_pick(pick_page, lambda _r: None)

    assert pick_page.evaluate(
        "() => window.__pp_hover_label") == "卡片列表：6 条记录"


# ----------------------------------------------------------------------
# runner 集成：手写 flow step（list_mode=cards + fields）→ CSV 6 行 2 列
# ----------------------------------------------------------------------

def test_runner_flow_with_cards_step(cards_browser, card_site, tmp_path: Path):
    """手写卡片步整链重放：goto + table(cards) → 产物 CSV 表头+6 行 2 列。"""
    flow = {
        "version": 1, "name": "cards-flow", "site": "cards",
        "url": f"{card_site}/cards",
        "steps": [
            {"no": 1, "kind": "goto", "url": f"{card_site}/cards"},
            {"no": 2, "kind": "table", "selector": "div.list",
             "list_mode": "cards", "fields": [
                 {"label": "订单号", "rel": [{"tag": "div", "nth": 1}]},
                 {"label": "价格",
                  "rel": [{"tag": "div", "nth": 2}, {"tag": "span", "nth": 2}]},
             ]},
        ],
    }
    result = runner.run_flow(cards_browser, flow, tmp_path)

    assert result["ok"] is True and result["failed_step"] is None
    assert "抓卡片 6 行" in result["results"][1]["detail"]
    csv_text = (tmp_path / "cards-flow-2.csv").read_bytes().decode("utf-8-sig")
    lines = csv_text.splitlines()
    assert lines[0] == "订单号,价格"
    assert len(lines) == 7  # 表头 + 6 行
    assert lines[1] == "TB9001,99.0"
    assert lines[6] == "TB9006,499.0"


# ----------------------------------------------------------------------
# flows 校验兼容：list_mode/fields 规则（旧 step 无此键照收）
# ----------------------------------------------------------------------

def _flow(steps: list[dict]) -> dict:
    return {"version": 1, "name": "t", "site": "s", "url": "https://x.example",
            "steps": steps}


def test_save_flow_accepts_cards_step(tmp_path: Path):
    """cards 步带非空 fields → 收；落盘 roundtrip 保留 list_mode/fields。"""
    fields = [{"label": "订单号", "rel": [{"tag": "div", "nth": 1}]}]
    path = save_flow(tmp_path, _flow([
        {"kind": "table", "selector": "div.list",
         "list_mode": "cards", "fields": fields}]))
    step = load_flow(tmp_path, "t")["steps"][0]
    assert step["list_mode"] == "cards" and step["fields"] == fields
    assert path.is_file()


def test_save_flow_cards_without_fields_rejected(tmp_path: Path):
    """list_mode=cards 缺 fields → ValueError 点名 fields。"""
    with pytest.raises(ValueError, match="fields"):
        save_flow(tmp_path, _flow(
            [{"kind": "table", "selector": "div.list", "list_mode": "cards"}]))
    with pytest.raises(ValueError, match="fields"):
        save_flow(tmp_path, _flow([
            {"kind": "table", "selector": "div.list",
             "list_mode": "cards", "fields": []}]))


def test_save_flow_illegal_list_mode_rejected(tmp_path: Path):
    """list_mode 不在 ("table","cards") → ValueError 列出合法值。"""
    with pytest.raises(ValueError, match="list_mode") as ei:
        save_flow(tmp_path, _flow([
            {"kind": "table", "selector": "div.list", "list_mode": "grid"}]))
    assert "cards" in str(ei.value) and "table" in str(ei.value)


def test_save_flow_legacy_table_step_defaults(tmp_path: Path):
    """旧 step 无 list_mode/fields → 照收（缺省 table），零回归。"""
    save_flow(tmp_path, _flow([{"kind": "table", "selector": "#data"}]))
    assert load_flow(tmp_path, "t")["steps"][0]["selector"] == "#data"
