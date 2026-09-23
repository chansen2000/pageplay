"""CLI 单测（pick/run/recipes）：只测命令派发与退出码映射，不起真浏览器（真流程归 test_integration）。

替身页面/下载/替身 context 与 recipe 种子在本文件；SiteSession 替身与
"手写已登录现场"工具被 login/doctor 等测试共用，留在 test_cli 复用。
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pageplay import picker
from pageplay.cli import main
from pageplay.recipes import save_recipe

from test_cli import _write_saved_site, install_stub_session


# ----------------------------------------------------------------------
# v0.2 pick/run/recipes：替身页面（不起真浏览器，真流程归 test_integration）
# ----------------------------------------------------------------------

class PickPage:
    """pick 替身页面：goto 记录 + 固定的最终落点 url。"""

    def __init__(self, landed_url: str) -> None:
        self.url = landed_url
        self.goto_urls: list[str] = []

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)


class RunPage:
    """run 替身页面：风控文本、选择器等待、页面上下文读表全替身。"""

    def __init__(self, table: dict | None = None,
                 wait_error: Exception | None = None) -> None:
        self.goto_urls: list[str] = []
        self.table = table or {}
        self.wait_error = wait_error

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)

    def content(self) -> str:
        return "<html><body>正常页面内容</body></html>"

    def wait_for_selector(self, selector: str, timeout: int) -> None:
        if self.wait_error is not None:
            raise self.wait_error

    def eval_on_selector(self, selector: str, js: str) -> dict:
        return self.table


class FakeDownload:
    suggested_filename = "report.xlsx"

    def save_as(self, target) -> None:
        Path(target).write_bytes(b"fake-xlsx-content")


class DownloadPage(RunPage):
    """download 档替身：expect_download/click 按 playwright 调用形状模拟。"""

    def __init__(self) -> None:
        super().__init__()
        self.clicked: list[str] = []

    def expect_download(self):
        info = type("DlInfo", (), {"value": FakeDownload()})()
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield info

        return cm()

    def click(self, selector: str) -> None:
        self.clicked.append(selector)


class OnePageContext:
    """只产出一个既有页面的替身 context。"""

    def __init__(self, page) -> None:
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self) -> None:
        self.closed = True


def _seed_recipe(home_dir: Path, name: str, url: str, action: str,
                 selector: str, columns=None, site: str = "faketest") -> None:
    """手写已登录现场（meta.json）+ 经真实 save_recipe 落一个 recipe。"""
    _write_saved_site(home_dir, name=site)
    save_recipe(home_dir / "sites" / site, {
        "version": 1, "name": name, "site": site, "url": url,
        "action": action, "selector": selector, "columns": columns,
    })


def test_pick_full_flow_saves_recipe_and_cover(home_dir, monkeypatch, capsys):
    """pick 全流程：--name 生效，recipe json + 封面 png 落盘，打印 run 指引。"""
    _write_saved_site(home_dir, name="faketest")
    page = PickPage("https://demo.example.com/report")
    install_stub_session(monkeypatch, context=OnePageContext(page))

    def fake_run_pick(page, on_confirm):
        payload = {"selector": "#data", "action": "table",
                   "columns": ["名称"], "rect": {"x": 1}}
        on_confirm(payload)
        return payload

    monkeypatch.setattr("pageplay.picker.run_pick", fake_run_pick)
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))

    assert main(["pick", "faketest", "--name", "my-recipe"]) == 0

    recipe = json.loads((home_dir / "sites" / "faketest" / "recipes"
                         / "my-recipe.json").read_text(encoding="utf-8"))
    assert recipe["version"] == 1
    assert recipe["name"] == "my-recipe" and recipe["site"] == "faketest"
    assert recipe["url"] == "https://demo.example.com/report"  # 实际落点
    assert recipe["action"] == "table" and recipe["selector"] == "#data"
    assert recipe["columns"] == ["名称"]
    cover = home_dir / "sites" / "faketest" / "recipes" / "my-recipe.png"
    assert cover.read_bytes() == b"png"
    assert page.goto_urls == ["https://demo.example.com/login"]  # 开 home_url
    out = capsys.readouterr().out
    assert "已锁定 table：#data" in out      # on_confirm 钩子回显
    assert "pageplay run my-recipe" in out   # run 指引


def test_pick_default_name_counts_existing_recipes(home_dir, monkeypatch, capsys):
    """无 --name：默认名 = <站点>-<已有 recipe 数 + 1>；download 列为 None。"""
    _write_saved_site(home_dir, name="faketest")
    install_stub_session(
        monkeypatch, context=OnePageContext(PickPage("https://demo.example.com/x")))
    monkeypatch.setattr("pageplay.picker.run_pick",
                        lambda page, on_confirm: {"selector": "#t",
                                                  "action": "download",
                                                  "columns": None})
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))
    save_recipe(home_dir / "sites" / "faketest", {
        "version": 1, "name": "faketest-1", "site": "faketest",
        "url": "https://demo.example.com/x", "action": "table",
        "selector": "#t", "columns": None})

    assert main(["pick", "faketest"]) == 0

    recipe = json.loads((home_dir / "sites" / "faketest" / "recipes"
                         / "faketest-2.json").read_text(encoding="utf-8"))
    assert recipe["name"] == "faketest-2"    # 已有 1 个 → 序号 2
    assert recipe["action"] == "download" and recipe["columns"] is None


def test_pick_cancelled_returns_one(home_dir, monkeypatch, capsys):
    """Esc 取消：打印"已取消"退出码 1。"""
    _write_saved_site(home_dir, name="faketest")
    install_stub_session(
        monkeypatch, context=OnePageContext(PickPage("https://x/1")))

    def cancelled(page, on_confirm):
        raise picker.PickCancelled("人按 Esc 取消了框选")

    monkeypatch.setattr("pageplay.picker.run_pick", cancelled)

    assert main(["pick", "faketest"]) == 1
    assert "已取消" in capsys.readouterr().out


def test_run_table_recipe_saves_csv_and_json(home_dir, table_site, tmp_path,
                                             monkeypatch, capsys):
    """run 抓表：默认 headless，--out 生效，CSV+JSON 各 5 行、列过滤生效。"""
    _seed_recipe(home_dir, "faketest-table", table_site + "/table",
                 "table", "#data", columns=["名称", "价格"])
    table = {"headers": ["名称", "价格", "库存", "链接"],
             "data": [[f"商品{i}", str(i * 10), "1", "x"] for i in range(1, 6)]}
    page = RunPage(table=table)
    created = install_stub_session(monkeypatch, context=OnePageContext(page))
    out_dir = tmp_path / "产物"

    assert main(["run", "faketest-table", "--out", str(out_dir)]) == 0

    (session,) = created
    assert session.open_headless is True                  # 默认 headless
    assert page.goto_urls == [table_site + "/table"]
    csv_files = list(out_dir.glob("faketest-table-*.csv"))
    json_files = list(out_dir.glob("faketest-table-*.json"))
    assert len(csv_files) == len(json_files) == 1         # 文件名带时间戳
    rows = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert len(rows) == 5
    assert rows[0] == {"名称": "商品1", "价格": "10"}      # 只留勾选列
    assert str(csv_files[0].resolve()) in capsys.readouterr().out


def test_run_download_recipe_saves_file(home_dir, table_site, tmp_path, monkeypatch):
    """run 下载档：点击触发下载，文件按建议文件名落盘且内容一致。"""
    _seed_recipe(home_dir, "faketest-dl", table_site + "/download",
                 "download", "#dl")
    page = DownloadPage()
    install_stub_session(monkeypatch, context=OnePageContext(page))
    out_dir = tmp_path / "dl-out"

    assert main(["run", "faketest-dl", "--out", str(out_dir)]) == 0

    assert page.clicked == ["#dl"]
    assert (out_dir / "report.xlsx").read_bytes() == b"fake-xlsx-content"


def test_run_selector_timeout_prompts_repick(home_dir, table_site, monkeypatch, capsys):
    """选择器 15s 等不到：提示"重新 pick"退出码 1，不猜不硬跑。"""
    _seed_recipe(home_dir, "faketest-stale", table_site + "/table",
                 "table", "#gone")
    page = RunPage(wait_error=PlaywrightTimeoutError("Timeout 15000ms exceeded"))
    install_stub_session(monkeypatch, context=OnePageContext(page))

    assert main(["run", "faketest-stale"]) == 1

    assert "重新 pick" in capsys.readouterr().err


def test_run_unknown_recipe_lists_existing(home_dir, capsys):
    """recipe 名跨站都找不到：报错列出现有全部 recipe 名。"""
    _seed_recipe(home_dir, "faketest-table", "https://x/table", "table", "#data")

    assert main(["run", "no-such"]) == 1

    err = capsys.readouterr().err
    assert "不存在" in err and "faketest-table" in err


def test_recipes_lists_name_action_url_created(home_dir, capsys):
    """recipes：列名字/动作/URL/创建时间；带站点名只列该站。"""
    _seed_recipe(home_dir, "faketable", "https://demo.example.com/table",
                 "table", "#data", columns=["名称"])

    assert main(["recipes"]) == 0
    out = capsys.readouterr().out
    assert "faketable" in out and "table" in out
    assert "https://demo.example.com/table" in out
    assert "创建于" in out

    assert main(["recipes", "taobao"]) == 0   # taobao 无 recipe → 只见空态提示
    out2 = capsys.readouterr().out
    assert "faketable" not in out2 and "暂无 recipe" in out2


def test_recipes_empty_home_prints_hint(home_dir, capsys):
    """空态：一个 recipe 都没有时给 pick 指引，退出码 0。"""
    home_dir.mkdir(parents=True)

    assert main(["recipes"]) == 0
    assert "暂无 recipe" in capsys.readouterr().out
