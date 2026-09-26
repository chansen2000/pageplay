"""vision 本地引擎单测（T22）：聚类/空格收敛纯函数、真实 OCR、引擎路由。

从 test_vision.py 按引擎职责拆出（单文件 ≤500）：GLM 视觉路与其 CLI
集成留在 test_vision.py；这里放 T22 本地库优先的全部用例。共享替身
（ShotPage / VisionPage / 浏览器附着与 screenshot_table 打桩）与常量
从 test_vision 导入。

真实 OCR 用例用 playwright 真渲染截图 + 真 tesseract / rapidocr 跑通
全链，可能慢（单条秒级到十几秒），断言只锚稳定事实（订单号/价格/
文本集合），容忍个别单元格 OCR 缺字；不出本机、不出外网。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pageplay.cli import main
from pageplay.cli_pick import _runs_path
from pageplay.runs import load_runs
from pageplay import vision
from pageplay.vision import (extract_local, screenshot_local,
                             screenshot_table)

from fakes_browser import fake_downloads_home
from test_vision import (_KEY, _ROWS, ShotPage, VisionPage,
                         install_vision_browser, stub_vision)


# ----------------------------------------------------------------------
# 聚类 / 空格收敛纯函数（合成输入，快）
# ----------------------------------------------------------------------

def test_cluster_splits_columns_by_x_gap_and_pads_missing():
    """x 空隙 > 图宽 8% 切栏；栏内 y 聚类成行；某栏该行无文本补 ""。"""
    blocks = [(54, 52, "商品A"), (432, 52, "12.50"),
              (54, 94, "商品B"), (434, 94, "33.80"),
              (55, 138, "商品C")]  # 给的是阅读序，聚类按 cx 自排
    rows = vision._cluster_text_blocks(blocks, 800)
    assert rows == [
        {"栏1": "商品A", "栏2": "12.50"},
        {"栏1": "商品B", "栏2": "33.80"},
        {"栏1": "商品C", "栏2": ""},
    ]


def test_cluster_single_column_clusters_lines_by_y():
    """无 x 空隙即单栏：距行首块 cy ≤8px 并行（锚定行首不链式），否则新行。"""
    rows = vision._cluster_text_blocks(
        [(100, 10, "甲"), (100, 40, "乙"), (102, 44, "丙")], 800)
    assert rows == [{"栏1": "甲"}, {"栏1": "乙 丙"}]


def test_cluster_empty_blocks_empty_rows():
    """全文一个文本块都没有（空白图）：[]。"""
    assert vision._cluster_text_blocks([], 800) == []


def test_squeeze_cjk_spaces_only_between_hanzi():
    """只去两侧都是汉字的空格（tesseract 中文词间空格矫姿）；
    英文/数字间距与首尾空格保留。"""
    assert vision._squeeze_cjk_spaces("订单 号") == "订单号"
    assert vision._squeeze_cjk_spaces("机 械 键盘") == "机械键盘"
    assert vision._squeeze_cjk_spaces("商品A") == "商品A"  # 品|A 非 CJK-CJK
    assert vision._squeeze_cjk_spaces("iPhone 15 Pro") == "iPhone 15 Pro"
    assert vision._squeeze_cjk_spaces(" 已 发 货 ") == " 已发货 "


# ----------------------------------------------------------------------
# 真实渲染 + 真 OCR（可能慢，断言锚稳定事实）
# ----------------------------------------------------------------------

_TABLE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font-family:PingFang SC,monospace;margin:20px}
table{border-collapse:collapse}
td,th{border:1px solid #000;padding:6px 14px;font-size:16px}
</style></head><body>
<table>
<tr><th>订单号</th><th>商品</th><th>价格</th><th>状态</th></tr>
<tr><td>20260926001</td><td>无线鼠标</td><td>89.90</td><td>已发货</td></tr>
<tr><td>20260926002</td><td>机械键盘</td><td>329.00</td><td>已付款</td></tr>
<tr><td>20260926003</td><td>显示器支架</td><td>159.50</td><td>待付款</td></tr>
</table></body></html>"""

_TWOCOL_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font-family:PingFang SC,sans-serif;margin:30px;font-size:18px}
.wrap{display:flex;gap:180px}
.col{width:200px;line-height:2.4}
</style></head><body><div class="wrap">
<div class="col"><div>商品A</div><div>商品B</div><div>商品C</div></div>
<div class="col"><div>12.50</div><div>33.80</div><div>7.20</div></div>
</div></body></html>"""

_BLANK_HTML = "<html><body style='background:#ffffff'></body></html>"


def _render_png(html: str, tmp_path) -> str:
    """playwright 真渲染 HTML → 截图 PNG 路径（真实 OCR 用例的素材）。"""
    from playwright.sync_api import sync_playwright
    path = tmp_path / "shot.png"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 400})
        page.set_content(html)
        page.screenshot(path=str(path), type="png")
        browser.close()
    return str(path)


def test_extract_local_bordered_table(tmp_path):
    """有边框表格：img2table 认表、首行作列名、订单号列含预期值
    （真实渲染 + 真 tesseract，可能慢；容忍个别单元格 OCR 缺字）。"""
    rows = extract_local(_render_png(_TABLE_HTML, tmp_path))
    assert [r["订单号"] for r in rows] == \
        ["20260926001", "20260926002", "20260926003"]
    assert rows[1]["商品"] == "机械键盘"
    assert rows[1]["价格"] == "329.00" and rows[2]["价格"] == "159.50"


def test_extract_local_borderless_two_columns(tmp_path):
    """无边框两栏：img2table 找不到表 → rapidocr 全文块聚类
    （真实渲染 + 真 rapidocr，可能慢；容忍"栏N"列名，断言文本全在）。"""
    rows = extract_local(_render_png(_TWOCOL_HTML, tmp_path))
    values = {cell for row in rows for cell in row.values()}
    for text in ("商品A", "商品B", "商品C", "12.50", "33.80", "7.20"):
        assert text in values
    assert set(rows[0]) == {"栏1", "栏2"}  # 列名是"栏N"口径


def test_extract_local_blank_image_returns_empty(tmp_path):
    """纯色空白图：两级都识别不出 → []（真实 OCR，可能慢）。"""
    assert extract_local(_render_png(_BLANK_HTML, tmp_path)) == []


def test_screenshot_local_writes_png_tempfile():
    """png 字节 → .png 后缀临时文件，内容原样（调用方负责删）。"""
    path = screenshot_local(ShotPage())
    try:
        assert Path(path).suffix == ".png"
        assert Path(path).read_bytes() == b"png-bytes"
    finally:
        Path(path).unlink(missing_ok=True)


# ----------------------------------------------------------------------
# screenshot_table 引擎路由
# ----------------------------------------------------------------------

def test_screenshot_table_local_default_no_glm_key(monkeypatch):
    """缺省 engine=local：环境无 GLM_API_KEY 也走本地；extract_local 收到
    真实落盘的临时 png（桩内趁文件还在读内容），用完即删。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    seen: list[tuple[str, bytes]] = []

    def fake_extract(png_path: str) -> list[dict]:
        seen.append((png_path, Path(png_path).read_bytes()))
        return _ROWS

    monkeypatch.setattr(vision, "extract_local", fake_extract)

    assert screenshot_table(ShotPage()) == _ROWS

    png_path, content = seen[0]
    assert content == b"png-bytes"
    assert Path(png_path).suffix == ".png"
    assert not Path(png_path).exists()  # 用完即删（识别失败也删的 finally）


def test_screenshot_table_glm_engine_runs_glm_flow(monkeypatch):
    """engine="glm" 显式指名走既有 GLM 流程（T22 起缺省是 local）。"""
    monkeypatch.setenv("GLM_API_KEY", _KEY)
    seen_models: list[str] = []
    monkeypatch.setattr(
        vision, "_post",
        lambda p, k: seen_models.append(p["model"]) or "{\"rows\": []}")
    assert screenshot_table(ShotPage(), engine="glm") == []
    assert seen_models == ["glm-4v-plus"]


def test_screenshot_table_unknown_engine_valueerror():
    """非法 engine：ValueError 人话点名可选值，且不截屏不发请求。"""
    page = ShotPage()
    with pytest.raises(ValueError, match="未知的视觉引擎"):
        screenshot_table(page, engine="magic")
    assert page.calls == []  # 路由先于截屏


# ----------------------------------------------------------------------
# CLI：--engine 透传与非法值人话
# ----------------------------------------------------------------------

def test_vision_engine_glm_flag_reaches_engine(home_dir, tmp_path,
                                               monkeypatch):
    """--engine glm：engine 原样传给 screenshot_table（打桩核对）。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])
    _, engines = stub_vision(monkeypatch, _ROWS)

    assert main(["vision", "taobao", "--engine", "glm"]) == 0
    assert engines == ["glm"]


def test_vision_unknown_engine_human_error_exit_one(home_dir, tmp_path,
                                                    monkeypatch, capsys):
    """非法 engine：真 screenshot_table 先校验 → ValueError 人话退出 1，
    配置问题不落账（也不需要 key——路由校验先于 key 校验）。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    assert main(["vision", "taobao", "--engine", "magic"]) == 1

    assert "未知的视觉引擎" in capsys.readouterr().err
    assert load_runs(_runs_path()) == []
