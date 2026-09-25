"""actions 执行器测试：风控检查与落盘为纯逻辑；抽表/下载走真 chromium。

前置：.venv/bin/python -m playwright install chromium
内核/驱动起不来时如实 pytest.skip，不假绿（风格同 test_integration）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pageplay.actions import (
    check_page_risk,
    download_element,
    extract_cards,
    extract_table,
    file_size_str,
    save_table,
)
from pageplay.guard import RiskTriggered

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402


# ---------------------------------------------------------------------------
# check_page_risk（纯逻辑）
# ---------------------------------------------------------------------------

def test_check_page_risk_hits_slider_keyword():
    """响应含"滑块"→ RiskTriggered。"""
    with pytest.raises(RiskTriggered, match="滑块"):
        check_page_risk("系统检测到异常，请完成滑块验证后继续")


def test_check_page_risk_passes_normal_text():
    """正常页面文本放行（不抛即过）。"""
    check_page_risk("<html><body>订单列表：共 128 条记录</body></html>")


# ---------------------------------------------------------------------------
# save_table（纯逻辑：目录创建 + 双格式落盘）
# ---------------------------------------------------------------------------

def test_save_table_writes_csv_and_json(tmp_path: Path):
    """落盘 <stem>.csv(utf-8-sig)+<stem>.json；列序取首行键序；目录不存在建。"""
    rows = [
        {"名称": "商品1", "价格": "10", "库存": "100"},
        {"名称": "商品2", "价格": "20", "库存": "90"},
    ]
    out_dir = tmp_path / "nested" / "out"  # 故意不预建目录
    csv_path, json_path = save_table(rows, out_dir, "report")

    assert csv_path == out_dir / "report.csv"
    assert json_path == out_dir / "report.json"

    text = csv_path.read_bytes().decode("utf-8-sig")  # BOM 由该解码器剥离
    lines = text.splitlines()
    assert lines[0] == "名称,价格,库存"  # csv 首行表头，键序保留
    assert lines[1] == "商品1,10,100"
    assert lines[2] == "商品2,20,90"

    assert json.loads(json_path.read_text(encoding="utf-8")) == rows


def test_save_table_empty_rows(tmp_path: Path):
    """空结果集：json 为 []，csv 只有表头行。"""
    csv_path, json_path = save_table([], tmp_path, "empty")
    assert json.loads(json_path.read_text(encoding="utf-8")) == []
    assert csv_path.read_bytes().decode("utf-8-sig").strip() == ""


# ---------------------------------------------------------------------------
# file_size_str（纯逻辑：产物大小人话，T9b）
# ---------------------------------------------------------------------------

def test_file_size_str_bytes_kb_mb(tmp_path: Path):
    """三档：<1KB 整数字节；KB/MB 一位小数（1024 进位）。"""
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 812)
    assert file_size_str(small) == "812 B"

    mid = tmp_path / "mid.bin"
    mid.write_bytes(b"x" * 1229)  # 1229 / 1024 = 1.2 KB
    assert file_size_str(mid) == "1.2 KB"

    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 3_565_158)  # 3.4 MB
    assert file_size_str(big) == "3.4 MB"


def test_file_size_str_missing_file_returns_zero_bytes(tmp_path: Path):
    """文件不存在 → "0 B"，不抛。"""
    assert file_size_str(tmp_path / "no-such.bin") == "0 B"


# ---------------------------------------------------------------------------
# extract_table / download_element（真 chromium + table_site）
# ---------------------------------------------------------------------------

_ROW0_FULL = {"名称": "商品1", "价格": "10", "库存": "100", "链接": "商品1"}


@pytest.fixture
def real_page(table_site):
    """真 chromium 起一个 headless context + 空白页（不导航），内核缺失时 skip。"""
    try:
        pw = sync_playwright().start()
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过：{exc}")
    try:
        context = pw.chromium.launch(headless=True)
    except Exception as exc:
        pw.stop()
        pytest.skip(f"playwright chromium 不可用，跳过：{exc}")
    page = context.new_page()
    yield page
    context.close()
    pw.stop()


def test_extract_table_full_columns(real_page, table_site):
    """全列提取：5 行，首行四列值与假站数据一致。"""
    real_page.goto(f"{table_site}/table")
    rows = extract_table(real_page, "#data", None)
    assert len(rows) == 5
    assert rows[0] == _ROW0_FULL
    assert rows[4] == {"名称": "商品5", "价格": "50", "库存": "60", "链接": "商品5"}


def test_extract_table_column_filter(real_page, table_site):
    """columns 过滤：只留指定列，顺序按 columns 给定序。"""
    real_page.goto(f"{table_site}/table")
    rows = extract_table(real_page, "#data", ["价格", "名称"])
    assert rows[0] == {"价格": "10", "名称": "商品1"}
    assert list(rows[0].keys()) == ["价格", "名称"]


def test_extract_table_missing_column_raises(real_page, table_site):
    """请求不存在的列 → ValueError 点名缺的列。"""
    real_page.goto(f"{table_site}/table")
    with pytest.raises(ValueError, match="销量"):
        extract_table(real_page, "#data", ["名称", "销量"])


def test_extract_table_with_risk_check(real_page, table_site):
    """执行器组合行为：页面文本先过风控再抽表（run 流的关键顺序）。"""
    real_page.goto(f"{table_site}/table")
    check_page_risk(real_page.content())  # 正常页放行
    rows = extract_table(real_page, "#data", ["名称", "价格"])
    assert rows[2] == {"名称": "商品3", "价格": "30"}


def test_download_element_saves_file(real_page, table_site, tmp_path):
    """点击导出链接：按 Content-Disposition 命名落盘，字节体一致；目录不存在建。"""
    real_page.goto(f"{table_site}/download")
    target = download_element(real_page, "#dl", tmp_path / "dl")
    assert target == tmp_path / "dl" / "report.xlsx"
    assert target.read_bytes() == b"fake-xlsx-content"


# ---------------------------------------------------------------------------
# extract_cards（真 chromium + card_site，v0.7-A 卡片列表）
# ---------------------------------------------------------------------------

# 与 conftest _CARDS_HTML 结构逐字对应：订单号 = 卡片第 1 个 div；
# 价格 = 第 2 个 div 里的第 2 个 span
_CARDS_FIELDS = [
    {"label": "订单号", "rel": [{"tag": "div", "nth": 1}]},
    {"label": "价格", "rel": [{"tag": "div", "nth": 2}, {"tag": "span", "nth": 2}]},
]


def test_extract_cards_two_fields(real_page, card_site):
    """6 张卡片 × 勾选 2 字段：6 行 dict，rel 相对链逐条解析命中。"""
    real_page.goto(f"{card_site}/cards")
    rows = extract_cards(real_page, "div.list", _CARDS_FIELDS)
    assert len(rows) == 6
    assert rows[0] == {"订单号": "TB9001", "价格": "99.0"}
    assert rows[5] == {"订单号": "TB9006", "价格": "499.0"}


def test_extract_cards_empty_fields_raises(real_page, card_site):
    """fields 为空 → ValueError（至少勾选一个字段）。"""
    real_page.goto(f"{card_site}/cards")
    with pytest.raises(ValueError, match="fields"):
        extract_cards(real_page, "div.list", [])


def test_extract_cards_no_card_group_raises(real_page, card_site):
    """容器内无同签名记录组（h1 无子元素）→ ValueError 人话带重新框选指引。"""
    real_page.goto(f"{card_site}/cards")
    with pytest.raises(ValueError, match="重新框选"):
        extract_cards(real_page, "h1", _CARDS_FIELDS)
