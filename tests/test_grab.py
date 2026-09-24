"""grab 命令单测（T14）：取当前页框选、执行落账不存 recipe、空页人话。

不起真浏览器：浏览器附着在 cli_grab 命名空间打桩（替身 context 自带
pages 列表，最后一个 = 用户当前页）；picker.run_pick 打桩替人确认，
确认载荷走真 actions.extract_table / download_element → 真 CSV/JSON
落盘 → 真 runs.jsonl 账本。零 recipe 是 grab 的契约，逐条断言。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from pageplay.cli import main
from pageplay.cli_pick import _runs_path
from pageplay.guard import RiskTriggered
from pageplay.picker import PickCancelled
from pageplay.runs import load_runs

from fakes_browser import fake_downloads_home

_TABLE = {"headers": ["名称", "价格", "库存"],
          "data": [["商品1", "10", "1"], ["商品2", "20", "2"]]}

_URL = "https://demo.example.com/list"


class FakeDownload:
    suggested_filename = "report.xlsx"

    def save_as(self, target) -> None:
        Path(target).write_bytes(b"fake-xlsx-content")


class GrabPage:
    """当前页替身：title()/url + 抽表/下载能力；记录 close（grab 不该关它）。"""

    def __init__(self, url: str = _URL, title: str = "列表页",
                 table: dict | None = None) -> None:
        self.url = url
        self._title = title
        self.table = table or _TABLE
        self.closed = False
        self.clicked: list[str] = []

    def title(self) -> str:
        return self._title

    def eval_on_selector(self, selector: str, js: str) -> dict:
        return self.table

    def expect_download(self):
        info = type("DlInfo", (), {"value": FakeDownload()})()

        @contextmanager
        def cm():
            yield info

        return cm()

    def click(self, selector: str) -> None:
        self.clicked.append(selector)

    def close(self) -> None:
        self.closed = True


class PagesContext:
    """自带 pages 列表的替身 context（CDP 附着形状：不产新页）。"""

    def __init__(self, pages: list) -> None:
        self.pages = pages


class PagesBrowser:
    def __init__(self, pages: list) -> None:
        self.contexts = [PagesContext(pages)]


def install_grab_browser(monkeypatch, pages: list) -> list[bool]:
    """把 cli_grab 的浏览器附着换成替身，返回记录的 headless 实参列表。"""
    headless_calls: list[bool] = []
    browser = PagesBrowser(pages)

    def fake_ensure(headless: bool = False):
        headless_calls.append(headless)
        return browser

    monkeypatch.setattr("pageplay.cli_grab.ensure_browser", fake_ensure)
    return headless_calls


def fake_confirm(action: str = "table", columns=None):
    """造一个"替人确认一次"的 run_pick 替身（断言单条模式）。"""
    def _run_pick(page, on_confirm, repeat=False):
        assert repeat is False  # grab 单条模式，不开框选会话
        on_confirm({"selector": "#t1", "action": action,
                    "columns": columns, "rect": {}, "url": page.url})
        return {"selector": "#t1"}

    return _run_pick


# ----------------------------------------------------------------------
# 主路径：取最后一个标签页，确认即执行落账，零 recipe
# ----------------------------------------------------------------------

def test_grab_takes_last_page_executes_and_records(home_dir, tmp_path,
                                                   monkeypatch, capsys):
    """取 pages 最后一个：确认即抽两列落 CSV/JSON，账本记 grab，无 recipe。"""
    fake_home = fake_downloads_home(monkeypatch, tmp_path)
    old_page, current = GrabPage("https://demo.example.com/old"), GrabPage()
    headless_calls = install_grab_browser(monkeypatch, [old_page, current])
    monkeypatch.setattr("pageplay.picker.run_pick",
                        fake_confirm(columns=["名称", "价格"]))

    assert main(["grab", "faketest"]) == 0

    assert headless_calls == [False]              # 附着有头活窗（不换守护）
    out = capsys.readouterr().out
    assert "将从此页取数据：列表页" in out and _URL in out
    grab_root = fake_home / "Downloads" / "pageplay" / "grab"
    (grab_dir,) = grab_root.glob("*-faketest")    # <时间戳>-<站>
    products = sorted(grab_dir.glob("faketest-grab-*.csv"))
    (csv_path,) = products
    assert csv_path.with_suffix(".json").is_file()
    assert csv_path.read_text(encoding="utf-8-sig").splitlines()[0] \
        == "名称,价格"                            # 两列过滤真的生效
    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "faketest-grab" and entry["action"] == "grab"
    assert entry["status"] == "ok" and len(entry["outputs"]) == 2
    assert not (home_dir / "sites" / "faketest" / "recipes").exists()
    assert current.closed is False                # 不关用户正在看的页


def test_grab_without_site_labels_from_page_url(home_dir, tmp_path,
                                                monkeypatch, capsys):
    """不带站点参数：标签从当前页 URL 推（item.taobao.com → taobao）。"""
    fake_downloads_home(monkeypatch, tmp_path)
    page = GrabPage("https://item.taobao.com/s?ie=utf8")
    install_grab_browser(monkeypatch, [page])
    monkeypatch.setattr("pageplay.picker.run_pick", fake_confirm())

    assert main(["grab"]) == 0

    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "taobao-grab"       # 域名推导只影响命名
    assert "将从此页取数据" in capsys.readouterr().out


def test_grab_download_action_saves_file(home_dir, tmp_path, monkeypatch):
    """元素档（download）：点击下载落盘，同一条 grab 账。"""
    fake_downloads_home(monkeypatch, tmp_path)
    page = GrabPage("https://demo.example.com/file")
    install_grab_browser(monkeypatch, [page])
    monkeypatch.setattr("pageplay.picker.run_pick",
                        fake_confirm(action="download", columns=None))

    assert main(["grab", "faketest"]) == 0

    (entry,) = load_runs(_runs_path())
    assert entry["action"] == "grab" and entry["status"] == "ok"
    assert Path(entry["outputs"][0]).name == "report.xlsx"


# ----------------------------------------------------------------------
# 异常路径：空页人话 / 取消 / 执行失败 / 真风控
# ----------------------------------------------------------------------

def test_grab_zero_pages_human_message_exit_one(home_dir, tmp_path,
                                                monkeypatch, capsys):
    """没有任何标签页：人话指引先开窗口，退出码 1。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_grab_browser(monkeypatch, [])

    assert main(["grab"]) == 1

    assert "先打开窗口" in capsys.readouterr().err


def test_grab_cancelled_exit_one(home_dir, tmp_path, monkeypatch, capsys):
    """人 Esc/超时取消框选：已取消，退出码 1。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_grab_browser(monkeypatch, [GrabPage()])

    def cancelled(page, on_confirm, repeat=False):
        raise PickCancelled("人按 Esc 取消了框选")

    monkeypatch.setattr("pageplay.picker.run_pick", cancelled)
    assert main(["grab"]) == 1
    assert "已取消" in capsys.readouterr().out


def test_grab_execution_fail_exit_one_and_fail_record(home_dir, tmp_path,
                                                      monkeypatch, capsys):
    """确认了但抽表失败（列不存在）：✗ 人话 + fail 账，退出码 1。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_grab_browser(monkeypatch, [GrabPage()])
    monkeypatch.setattr("pageplay.picker.run_pick",
                        fake_confirm(columns=["不存在的列"]))

    assert main(["grab"]) == 1

    assert "不存在的列" in capsys.readouterr().err
    (entry,) = load_runs(_runs_path())
    assert entry["action"] == "grab" and entry["status"] == "fail"


def test_grab_risk_exits_two(home_dir, tmp_path, monkeypatch):
    """真风控从执行路径穿透：RiskTriggered 不吞，main 映射退出码 2。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_grab_browser(monkeypatch, [GrabPage()])

    def risk_table(page, selector, columns):
        raise RiskTriggered("响应命中风控关键词：滑块")

    monkeypatch.setattr("pageplay.picker.run_pick", fake_confirm())
    monkeypatch.setattr("pageplay.actions.extract_table", risk_table)

    assert main(["grab"]) == 2
    assert load_runs(_runs_path()) == []          # 风控穿透，不落 fail 账


@pytest.mark.parametrize("bad", ["", "   "])
def test_grab_site_blank_treated_as_absent(home_dir, tmp_path, monkeypatch,
                                           bad):
    """站点参数给成空白等价于没给：标签回落当前页 URL 推导。"""
    fake_downloads_home(monkeypatch, tmp_path)
    page = GrabPage("https://item.taobao.com/s")
    install_grab_browser(monkeypatch, [page])
    monkeypatch.setattr("pageplay.picker.run_pick", fake_confirm())

    assert main(["grab", bad]) == 0

    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "taobao-grab"
