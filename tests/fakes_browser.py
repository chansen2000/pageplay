"""pick/run 共用替身：附着形状的替身浏览器/页面 + 产物目录与种子工具。

T10 后 pick/run 不再构造 SiteSession，而是经 cli_pick 的
ensure_browser / ensure_headful_browser 附着替身浏览器（install_browser）；
替身页面回答反弹判定（url）、风控文本（content）、抽表（eval_on_selector）。
风控协作 ensure_logged_in 由各测试文件在 cli_pick 命名空间自行打桩。
"""

from __future__ import annotations

from pathlib import Path

from test_cli import _write_saved_site

_TABLE = {"headers": ["名称", "价格", "库存", "链接"],
          "data": [[f"商品{i}", str(i * 10), "1", "x"] for i in range(1, 6)]}


class PickPage:
    """pick 替身页面：goto 记录 + 固定最终落点 url；带表数据可当场执行。"""

    def __init__(self, landed_url: str, table: dict | None = None) -> None:
        self.url = landed_url
        self.goto_urls: list[str] = []
        self.table = table or {}
        self.closed = False

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)

    def eval_on_selector(self, selector: str, js: str) -> dict:
        return self.table

    def close(self) -> None:
        self.closed = True


class RunPage:
    """run 替身页面：落点 url、风控文本、选择器等待、页面上下文读表全替身。"""

    def __init__(self, table: dict | None = None,
                 wait_error: Exception | None = None,
                 landed_url: str = "https://demo.example.com/table",
                 content: str = "<html><body>正常页面内容</body></html>") -> None:
        self.url = landed_url
        self.goto_urls: list[str] = []
        self.table = table or {}
        self.wait_error = wait_error
        self._content = content
        self.closed = False

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)

    def content(self) -> str:
        return self._content

    def wait_for_selector(self, selector: str, timeout: int) -> None:
        if self.wait_error is not None:
            raise self.wait_error

    def eval_on_selector(self, selector: str, js: str) -> dict:
        return self.table

    def close(self) -> None:
        self.closed = True


class BounceRunPage(RunPage):
    """登录反弹替身：恢复前每次 goto 都落在登录页，恢复后落目标页。"""

    def __init__(self, target_url: str, login_url: str) -> None:
        super().__init__(table=_TABLE, landed_url=target_url)
        self.target_url = target_url
        self.login_url = login_url
        self.recovered = False

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)
        self.url = self.target_url if self.recovered else self.login_url


class FakeDownload:
    suggested_filename = "report.xlsx"

    def save_as(self, target) -> None:
        Path(target).write_bytes(b"fake-xlsx-content")


class DownloadPage(RunPage):
    """download 档替身：expect_download/click 按 playwright 调用形状模拟。"""

    def __init__(self, download_error: Exception | None = None,
                 landed_url: str = "https://demo.example.com/download") -> None:
        super().__init__(landed_url=landed_url)  # pick 确认钩子记 recipe 落点用
        self.clicked: list[str] = []
        self.download_error = download_error

    def expect_download(self):
        if self.download_error is not None:
            raise self.download_error  # 点击后迟迟无下载事件的真实形状
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


class OnePageBrowser:
    """只带一个 context 的替身浏览器（CDP 附着形状：browser.contexts[0]）。"""

    def __init__(self, page) -> None:
        self.contexts = [OnePageContext(page)]


def install_browser(monkeypatch, page) -> list[bool]:
    """把 cli_pick 的浏览器附着函数换成替身，返回记录的 headless 实参列表。"""
    headless_calls: list[bool] = []
    browser = OnePageBrowser(page)

    def fake_ensure(headless: bool = False):
        headless_calls.append(headless)
        return browser

    monkeypatch.setattr("pageplay.cli_pick.ensure_browser", fake_ensure)
    monkeypatch.setattr("pageplay.cli_pick.ensure_headful_browser",
                        lambda: browser)
    return headless_calls


def fake_downloads_home(monkeypatch, tmp_path: Path) -> Path:
    """把 Path.home() 指到临时目录：pick 即时执行的产物不落真实 ~/Downloads。"""
    fake_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def seed_recipe(home_dir: Path, name: str, url: str, action: str,
                selector: str, columns=None, site: str = "faketest") -> None:
    """手写已登录现场（meta.json）+ 经真实 save_recipe 落一个 recipe。"""
    _write_saved_site(home_dir, name=site)
    from pageplay.recipes import save_recipe

    save_recipe(home_dir / "sites" / site, {
        "version": 1, "name": name, "site": site, "url": url,
        "action": action, "selector": selector, "columns": columns,
    })
