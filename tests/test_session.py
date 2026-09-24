"""SiteSession 单测：打桩 browser_factory（共享常驻浏览器替身），不碰真浏览器。

T10 常驻模型：SiteSession 只在共享浏览器里开页干活、完事关页——替身
FakeBrowser.contexts[0] 即共享上下文；cookies 走真实实现（T1c 已就位），
time.sleep 打桩为空操作让轮询瞬时跑完。守护进程本体（session.json/
CDP 附着/关停）归 test_daemon.py 的真浏览器往返测试。
"""

from __future__ import annotations

import json
from pathlib import Path

from pageplay.session import SiteSession
from pageplay.sites import SitePreset

SITE = SitePreset(
    name="taobao",
    login_url="https://login.example.com/member/login.jhtml",
    home_url="https://www.example.com",
    domain="taobao.com",
    check_cookies=("_tb_token_",),
)


def make_cookie(name: str, domain: str = "taobao.com") -> dict:
    return {"name": name, "value": "v-" + name, "domain": domain, "path": "/",
            "expires": -1, "httpOnly": False, "secure": False}


class FakePage:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    def goto(self, url: str) -> None:
        self.urls.append(url)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """cookies() 逐轮返回 batches，最后一批永久重复（共享上下文替身）。"""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches) or [[]]
        self.pages: list[FakePage] = []
        self.context_closed = False  # 契约：任何代码路径都不许关上下文

    def cookies(self) -> list[dict]:
        if len(self._batches) > 1:
            return self._batches.pop(0)
        return self._batches[0]

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.context_closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]
        self.close_calls = 0  # 契约：任何代码路径都不许关守护浏览器

    def close(self) -> None:
        self.close_calls += 1


class FactoryRecorder:
    """browser_factory 替身：记录 (headless) 调用并返回同一 FakeBrowser。"""

    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.calls: list[bool] = []

    def __call__(self, headless: bool) -> FakeBrowser:
        self.calls.append(headless)
        return self.browser


def make_session(tmp_path: Path, recorder: FactoryRecorder) -> SiteSession:
    return SiteSession(SITE, tmp_path / "sites", browser_factory=recorder)


# ----------------------------------------------------------------------
# login（有头开页轮询；完事关页不关浏览器）
# ----------------------------------------------------------------------

def test_login_success_writes_state_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[], [make_cookie("_tb_token_")]])  # 第1轮空，第2轮有标记
    recorder = FactoryRecorder(FakeBrowser(context))
    session = make_session(tmp_path, recorder)

    assert session.login(timeout_sec=30) is True

    site_dir = tmp_path / "sites" / "taobao"
    state = json.loads((site_dir / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["cookies"] == [make_cookie("_tb_token_")]  # 只存本域 cookie

    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["site"] == "taobao"
    assert meta["login_url"] == SITE.login_url
    assert meta["check_cookies"] == ["_tb_token_"]
    assert "saved_at" in meta

    assert recorder.calls == [False, True]  # 登录必有头；成功后刷快照（附着，参数不影响）
    assert context.pages[0].urls == [SITE.login_url]       # 打开登录页
    assert context.pages[0].closed is True                 # 完事关页
    assert context.context_closed is False                 # 绝不关上下文
    assert recorder.browser.close_calls == 0               # 绝不关守护浏览器


def test_login_timeout_returns_false_without_files(tmp_path, monkeypatch):
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[]])  # 恒空
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    assert session.login(timeout_sec=0) is False

    site_dir = tmp_path / "sites" / "taobao"
    assert not (site_dir / "state.json").exists()
    assert not (site_dir / "meta.json").exists()
    assert context.pages[0].closed is True                 # 页照关
    assert context.context_closed is False                 # 浏览器不动


def test_login_with_url_opens_pasted_page(tmp_path, monkeypatch):
    """贴网址登录：打开贴的页面而非预设 login_url（原行为不变）。"""
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[make_cookie("_tb_token_")]])
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    assert session.login(timeout_sec=10, url="https://www.pasted.com") is True
    assert context.pages[0].urls == ["https://www.pasted.com"]


# ----------------------------------------------------------------------
# verify（冷检查 + 暖检查，全部在共享上下文里）
# ----------------------------------------------------------------------

def test_verify_true_with_marker_cookie(tmp_path):
    context = FakeContext([[make_cookie("_tb_token_")]])
    recorder = FactoryRecorder(FakeBrowser(context))
    session = make_session(tmp_path, recorder)

    assert session.verify() is True
    assert recorder.calls == [True, True]  # 验活无头；冷命中后刷快照（附着幂等）
    assert (tmp_path / "sites" / "taobao" / "state.json").exists()
    assert context.pages == []                             # 冷检查不开页
    assert context.context_closed is False


def test_verify_false_without_login_state(tmp_path, monkeypatch):
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[]])
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))
    assert session.verify() is False
    assert context.pages[0].closed is True                 # 暖页已收
    assert not (tmp_path / "sites" / "taobao" / "state.json").exists()


def test_verify_cold_fail_warm_relogin_success(tmp_path, monkeypatch):
    """冷检查空 → 暖检查打开落地页等到自动续登 → True + 快照已刷新。"""
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[], [make_cookie("_tb_token_")]])  # 冷查空，暖第1轮命中
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    assert session.verify() is True

    site_dir = tmp_path / "sites" / "taobao"
    state = json.loads((site_dir / "state.json").read_text(encoding="utf-8"))
    assert state["cookies"] == [make_cookie("_tb_token_")]  # 续登出的 cookie 已落盘
    assert context.pages[0].urls == [SITE.home_url]         # 暖级打开的是落地页
    assert context.pages[0].closed is True
    assert context.context_closed is False


def test_verify_cold_and_warm_both_fail(tmp_path, monkeypatch):
    """恒空：暖级确实走过（goto 落地页 + 轮询到点），两级全败 → False。"""
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[]])
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    assert session.verify() is False
    assert context.pages[0].urls == [SITE.home_url]  # 暖级走过
    assert not (tmp_path / "sites" / "taobao" / "state.json").exists()  # 没刷过快照


def test_verify_warm_goto_error_is_level_failure_not_crash(tmp_path, monkeypatch):
    """goto 超时等异常：暖级判败返回 False，不未捕获崩溃，页面照收。"""
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    context = FakeContext([[]])

    def timeout_goto(self, url: str) -> None:
        raise TimeoutError(f"page.goto: Timeout {url} exceeded")

    monkeypatch.setattr(FakePage, "goto", timeout_goto)
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    assert session.verify() is False
    assert context.pages[0].closed is True


# ----------------------------------------------------------------------
# refresh_snapshot / export_cookies / open / close / forget
# ----------------------------------------------------------------------

def test_refresh_snapshot_reads_context_and_filters_by_domain(tmp_path):
    """快照只存本站域 cookie：共享档案装所有站，他域 cookie 不进本站快照。"""
    context = FakeContext([[
        make_cookie("_tb_token_", domain="taobao.com"),
        make_cookie("other", domain="other-site.com"),   # 他域：不进快照
        make_cookie("sub", domain=".login.taobao.com"),  # 本域子域：保留
    ]])
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    session.refresh_snapshot()

    state = json.loads(
        (tmp_path / "sites" / "taobao" / "state.json").read_text(encoding="utf-8"))
    names = [c["name"] for c in state["cookies"]]
    assert names == ["_tb_token_", "sub"]
    assert context.pages == []                             # 不开页，上下文直读


def test_export_cookies_reads_snapshot_without_browser(tmp_path):
    site_dir = tmp_path / "sites" / "taobao"
    site_dir.mkdir(parents=True)
    (site_dir / "state.json").write_text(json.dumps({
        "version": 1, "site": "taobao", "saved_at": "2026-09-23T00:00:00",
        "cookies": [make_cookie("_tb_token_")],
    }), encoding="utf-8")

    def boom(headless: bool):  # export 不得碰浏览器
        raise AssertionError("export_cookies 被意外要求附着浏览器")

    session = SiteSession(SITE, tmp_path / "sites", browser_factory=boom)
    assert session.export_cookies() == [make_cookie("_tb_token_")]


def test_open_returns_shared_context_and_close_closes_pages_only(tmp_path):
    context = FakeContext([[]])
    recorder = FactoryRecorder(FakeBrowser(context))
    session = make_session(tmp_path, recorder)

    assert session.open() is context                       # 返回共享上下文
    assert context.pages == []                             # 不给 url：不开页（原行为）
    assert recorder.calls == [False]                       # open 默认有头

    session.open("https://www.newsite.com")                # 给 url：开页导航
    assert context.pages[0].urls == ["https://www.newsite.com"]

    session.close()
    assert context.pages[0].closed is True                 # 只关页
    assert context.context_closed is False                 # 不关上下文
    assert recorder.browser.close_calls == 0               # 不关浏览器
    session.close()                                        # 幂等


def test_open_headless_passthrough(tmp_path):
    """open(headless=True) 透传给 factory（无头场景）；缺省仍有头。"""
    context = FakeContext([[]])
    recorder = FactoryRecorder(FakeBrowser(context))
    session = make_session(tmp_path, recorder)

    session.open(headless=True)
    assert recorder.calls[-1] is True
    session.close()
    session.open()
    assert recorder.calls[-1] is False


def test_write_meta_lands_schema_file(tmp_path):
    """write_meta：公开方法，schema 同设计 §3，不依赖浏览器。"""
    def boom(headless: bool):
        raise AssertionError("write_meta 被意外要求附着浏览器")

    session = SiteSession(SITE, tmp_path / "sites", browser_factory=boom)
    site_dir = tmp_path / "sites" / "taobao"
    site_dir.mkdir(parents=True)

    session.write_meta()

    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["site"] == "taobao"
    assert meta["login_url"] == SITE.login_url
    assert meta["check_cookies"] == ["_tb_token_"]
    assert "saved_at" in meta


def test_forget_removes_site_dir(tmp_path):
    site_dir = tmp_path / "sites" / "taobao"
    site_dir.mkdir(parents=True)
    (site_dir / "state.json").write_text("{}", encoding="utf-8")
    context = FakeContext([[]])
    session = make_session(tmp_path, FactoryRecorder(FakeBrowser(context)))

    session.forget()
    assert not site_dir.exists()
    assert context.context_closed is False                 # 共享档案/浏览器不动
