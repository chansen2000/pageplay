"""v0.10 listscan 引擎测试：淘宝形态假页按 §8 口径断言 + 哈希变更重放 + 链接下载。

内核缺失时如实 pytest.skip，不假绿。假页（conftest.list_site）复刻真机
买家订单页形态（设计 §2）：哈希 class、表头兄弟、一单 1/2/3 件、价格 4 段
span、首单内嵌推荐区、display:none 文本、a[href] 标题。断言口径同 §8：
记录/子项识别、碎片合并成价、前两单逐格、推荐区与隐藏文本零泄漏、
选择器不写哈希；哈希变更用例证明旧 spec 重放不依赖构建哈希（§3.1）；
链接用例证明「像文件」边界与文件名优先级（§5）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pageplay import listscan
from pageplay.actions import download_links, extract_list, extract_links
from pageplay.guard import Guard
from tests.conftest import _FILES

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

RECORD_SELECTOR = 'div[class*="tradeContent"] > div[class*="tradeOrder"]'


@pytest.fixture
def page():
    """真 chromium headless 页，铺淘宝形态列表页；内核缺失如实 skip。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"playwright chromium 不可用，跳过 listscan 集成：{exc}")
    pg = browser.new_page()
    yield pg
    pg.close()
    browser.close()
    pw.stop()


@pytest.fixture
def scanned(page, list_site):
    """打开假页并跑一次 scan，返回 (page, scan 产物)。"""
    page.goto(f"{list_site}/list")
    res = listscan.scan(page, RECORD_SELECTOR)
    return page, res


def _spec(res: dict) -> dict:
    """scan 产物 → extract spec（§6 契约：record_selector/sub_sem/columns）。"""
    return {"record_selector": res["record_selector"],
            "sub_sem": res["sub_sem"],
            "columns": [{"key": c["key"], "name": c["name"],
                         "strip": c["strip"]} for c in res["columns"]]}


def _rowtext(row: dict) -> str:
    return " | ".join(str(v) for v in row.values())


# ----------------------------------------------------------------------
# scan：记录/子项识别与列统计（§3）
# ----------------------------------------------------------------------

def test_scan_finds_records_subitems_and_columns(scanned):
    """6 单全识别、子项=itemInfo、10 件=10 行；订单号列带前缀剥离；
    选择器不写哈希后缀（§3.1）。"""
    _page, res = scanned
    assert res["n_records"] == 6
    assert res["sub_sem"] == "itemInfo"
    assert res["n_items"] == 10
    assert "--" not in res["record_selector"]
    assert res["record_selector"] == RECORD_SELECTOR
    order_col = next(c for c in res["columns"] if c["name"] == "订单号")
    assert order_col["strip"] == "订单号:" and order_col["sample"] == "8801"
    # 每列均 ≥50% 行（推荐区必须已被淘汰，不会出现在 columns）
    assert all("推荐" not in c["name"] and "常买常逛" not in c["name"]
               for c in res["columns"])
    # C1 规则②：紧挨标签「实付款」命名价格列，标签列本身删除
    pay_col = next(c for c in res["columns"] if c["name"] == "实付款")
    assert pay_col["sample"] == "￥650.00"
    assert not any(c["sem"] == "payLabel" for c in res["columns"])


def test_scan_drops_recommendation_and_hidden_text(scanned):
    """首单内嵌推荐区（1/6 <50%）与 display:none 文本不进任何列。"""
    _page, res = scanned
    dropped_keys = " ".join(d["key"] for d in res["dropped"])
    assert "extBlank" in dropped_keys  # 推荐区键被丢弃
    kept = " ".join(c["key"] + c["name"] + c["sample"] for c in res["columns"])
    assert "extBlank" not in kept
    assert "推荐" not in kept and "常买常逛" not in kept
    assert "旺旺在线" not in kept


# ----------------------------------------------------------------------
# extract：§8 口径逐格断言（碎片合并/记录级字段逐行重复/链接列）
# ----------------------------------------------------------------------

def test_extract_rows_match_design_shape(scanned):
    """10 行 = itemInfo 总数；前 2 单逐格：订单号/日期/店铺/状态/实付款/
    单价/数量/规格；碎片合并成 ￥650.00/￥144.40/￥79.00/￥65.40。"""
    page, res = scanned
    rows = listscan.extract(page, _spec(res))

    assert len(rows) == 10
    r0 = rows[0]
    assert r0["订单号"] == "8801"  # 前缀已剥离成纯号
    assert any(v == "2026-09-21" for v in r0.values())
    assert any("敏光企业店" in str(v) for v in r0.values())
    assert any(v == "卖家已发货" for v in r0.values())
    assert any(v == "￥650.00" for v in r0.values())  # 4 段 span 合并
    assert any(v == "￥50.00" for v in r0.values())
    assert any(v == "x13" for v in r0.values())
    assert any("LSSPD-1.2" in str(v) for v in r0.values())
    # 记录级字段逐行重复：8802 的两行（行 2/行 3）
    two = [r for r in rows if r.get("订单号") == "8802"]
    assert len(two) == 2
    assert any(v == "￥144.40" for v in two[0].values())
    assert "30CM" in _rowtext(two[0]) and "￥79.00" in _rowtext(two[0])
    assert "20CM" in _rowtext(two[1]) and "￥65.40" in _rowtext(two[1])
    # 商品标题链接列：绝对 URL
    link_cols = [c for c in res["columns"] if c["key"].endswith("_链接")]
    assert link_cols
    assert any(str(v).startswith("http") for r in rows for v in r.values())


def test_extract_forbidden_strings_absent(scanned):
    """全部行不含推荐区文字与旺旺在线（§8 反向口径）。"""
    page, res = scanned
    rows = listscan.extract(page, _spec(res))
    all_text = " ".join(_rowtext(r) for r in rows)
    for bad in ("推荐", "常买常逛", "旺旺在线"):
        assert bad not in all_text


def test_extract_requires_columns(scanned):
    """columns 为空 → ValueError（至少勾选一列）。"""
    page, _res = scanned
    with pytest.raises(ValueError, match="列"):
        extract_list(page, RECORD_SELECTOR, "itemInfo", [])


def test_extract_all_empty_rows_raises(scanned):
    """选择器锁到无文本的元素（全空行）→ ValueError 不产空产物。"""
    page, _res = scanned
    with pytest.raises(ValueError, match="重新框选"):
        extract_list(page, "script", None,
                     [{"key": "x", "name": "列"}])


# ----------------------------------------------------------------------
# 哈希变更：同页把 --xxxx 后缀全换，旧 spec 重放仍取到同样的行（§3.1）
# ----------------------------------------------------------------------

_REHASH_JS = """
() => {
  let seed = 7;
  const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648).toString(36);
  for (const el of document.querySelectorAll("[class]")) {
    el.className = el.className.split(" ").map(c => {
      const i = c.indexOf("--");
      return i > 0 ? c.slice(0, i) + "--" + rnd() + "Zz" : c;
    }).join(" ");
  }
}
"""


def test_hash_change_replay_same_rows(scanned):
    """哈希全换后用旧 spec 重放：行集完全一致（选择器/键都不含哈希）。"""
    page, res = scanned
    spec = _spec(res)
    rows_before = listscan.extract(page, spec)
    page.evaluate(_REHASH_JS)
    rows_after = listscan.extract(page, spec)
    assert rows_after == rows_before
    assert len(rows_after) == 10


# ----------------------------------------------------------------------
# 链接：抓取去重 + 「像文件」下载边界与文件名优先级（§4/§5）
# ----------------------------------------------------------------------

@pytest.fixture
def links_page(file_site):
    """真 chromium headless 页，打开文件假站的链接页。"""
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"playwright chromium 不可用，跳过链接下载集成：{exc}")
    pg = browser.new_page()
    pg.goto(f"{file_site}/links")
    yield pg
    pg.close()
    browser.close()
    pw.stop()


def test_extract_links_dedup_and_flag(links_page):
    """框内全部 a[href] 去重成 4 行；下载属性如实标记。"""
    rows = extract_links(links_page, "#box")
    assert len(rows) == 4  # a.pdf 出现两次，按 href 去重
    assert {r["文字"] for r in rows} == {"报表A", "报表B", "打包C", "普通链接（不像文件）"}
    by_text = {r["文字"]: r for r in rows}
    assert by_text["打包C"]["下载属性"] is True  # download 属性
    assert by_text["报表B"]["下载属性"] is False  # 靠扩展名
    assert by_text["普通链接（不像文件）"]["链接"].endswith("/home")


def test_download_links_only_file_like(links_page, tmp_path, monkeypatch):
    """只下 3 个像文件的链接；文件名 Content-Disposition > URL 末段；
    普通链接跳过；护栏 wait 在测试里短路。"""
    monkeypatch.setattr(Guard, "wait", lambda self: None)  # 测试不限速
    rows = extract_links(links_page, "#box")
    saved = download_links(links_page, rows, tmp_path)

    assert sorted(p.name for p in saved) == ["b.xlsx", "c.bin", "report-a.pdf"]
    by_name = {p.name: p for p in saved}
    assert by_name["report-a.pdf"].read_bytes() == _FILES["a.pdf"]  # 头里给的名
    assert by_name["b.xlsx"].read_bytes() == _FILES["b.xlsx"]       # URL 末段
    assert by_name["c.bin"].read_bytes() == _FILES["c.bin"]         # download 属性
    assert not (tmp_path / "home").exists()  # 普通链接不下载
