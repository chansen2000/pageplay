"""SiteSession 单测：全部打桩 launcher，不碰真浏览器。

依赖打桩说明：
- cookies 走真实实现（T1c 已就位，语义按契约），不 patch；
- time.sleep 打桩为空操作，让 login 轮询瞬时跑完；
- launcher 全部注入 FakeLauncher，Playwright 驱动无需安装。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

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

    def goto(self, url: str) -> None:
        self.urls.append(url)


class FakeContext:
    """cookies() 逐轮返回 batches，最后一批永久重复。"""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches) or [[]]
        self.pages: list[FakePage] = []
        self.closed = False

    def cookies(self) -> list[dict]:
        if len(self._batches) > 1:
            return self._batches.pop(0)
        return self._batches[0]

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class FakeLauncher:
    def __init__(self, context: FakeContext | None) -> None:
        self.context = context
        self.calls: list[tuple[Path, bool]] = []

    def __call__(self, user_data_dir: Path, headless: bool) -> FakeContext:
        self.calls.append((Path(user_data_dir), headless))
        assert self.context is not None, "launcher 被意外调用"
        return self.context


def make_session(tmp_path: Path, launcher) -> SiteSession:
    return SiteSession(SITE, tmp_path / "sites", launcher=launcher)


# ----------------------------------------------------------------------
# login
# ----------------------------------------------------------------------

def test_login_success_writes_state_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # 轮询瞬时
    context = FakeContext([[], [make_cookie("_tb_token_")]])  # 第1轮空，第2轮有标记
    launcher = FakeLauncher(context)
    session = make_session(tmp_path, launcher)

    assert session.login(timeout_sec=30) is True

    site_dir = tmp_path / "sites" / "taobao"
    state = json.loads((site_dir / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["cookies"] == [make_cookie("_tb_token_")]

    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["site"] == "taobao"
    assert meta["login_url"] == SITE.login_url
    assert meta["check_cookies"] == ["_tb_token_"]
    assert "saved_at" in meta

    assert launcher.calls == [(site_dir / "browser-profile", False)]  # headful
    assert context.pages[0].urls == [SITE.login_url]                  # 打开登录页
    assert context.closed is True                                     # finally 收尾
    assert not (site_dir / ".session.lock").exists()                  # 锁已清


def test_login_timeout_returns_false_without_files(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    context = FakeContext([[]])  # 恒空
    launcher = FakeLauncher(context)
    session = make_session(tmp_path, launcher)

    assert session.login(timeout_sec=0) is False

    site_dir = tmp_path / "sites" / "taobao"
    assert not (site_dir / "state.json").exists()
    assert not (site_dir / "meta.json").exists()
    assert context.closed is True
    assert not (site_dir / ".session.lock").exists()


# ----------------------------------------------------------------------
# verify
# ----------------------------------------------------------------------

def test_verify_true_with_marker_cookie(tmp_path):
    context = FakeContext([[make_cookie("_tb_token_")]])
    launcher = FakeLauncher(context)
    session = make_session(tmp_path, launcher)

    assert session.verify() is True
    assert launcher.calls == [(tmp_path / "sites" / "taobao" / "browser-profile", True)]
    assert context.closed is True
    assert not (tmp_path / "sites" / "taobao" / ".session.lock").exists()


def test_verify_false_without_login_state(tmp_path):
    session = make_session(tmp_path, FakeLauncher(FakeContext([[]])))
    assert session.verify() is False


# ----------------------------------------------------------------------
# refresh_snapshot / export_cookies / open / close / forget
# ----------------------------------------------------------------------

def test_refresh_snapshot_starts_headless_when_no_context(tmp_path):
    context = FakeContext([[make_cookie("_tb_token_")]])
    launcher = FakeLauncher(context)
    session = make_session(tmp_path, launcher)

    session.refresh_snapshot()

    assert launcher.calls == [(tmp_path / "sites" / "taobao" / "browser-profile", True)]
    assert (tmp_path / "sites" / "taobao" / "state.json").exists()
    assert context.closed is True  # 一次性：读完即关


def test_export_cookies_reads_snapshot_without_browser(tmp_path):
    site_dir = tmp_path / "sites" / "taobao"
    site_dir.mkdir(parents=True)
    (site_dir / "state.json").write_text(json.dumps({
        "version": 1, "site": "taobao", "saved_at": "2026-09-23T00:00:00",
        "cookies": [make_cookie("_tb_token_")],
    }), encoding="utf-8")
    session = make_session(tmp_path, FakeLauncher(None))  # 被调用即炸

    # export 不得启动浏览器：launcher 一旦被调用 FakeLauncher(None) 即断言失败
    assert session.export_cookies() == [make_cookie("_tb_token_")]


def test_open_returns_headful_context_and_close_releases(tmp_path):
    context = FakeContext([[]])
    launcher = FakeLauncher(context)
    session = make_session(tmp_path, launcher)

    assert session.open() is context
    assert launcher.calls == [(tmp_path / "sites" / "taobao" / "browser-profile", False)]
    assert context.pages == []  # 不给 url：不开页面、不导航（原行为）

    session.close()
    assert context.closed is True
    assert not (tmp_path / "sites" / "taobao" / ".session.lock").exists()


def test_open_with_url_navigates_page(tmp_path):
    """open(url)：起 context 后 new_page().goto(url)（贴网址登录入口）。"""
    context = FakeContext([[]])
    launcher = FakeLauncher(context)
    session = make_session(tmp_path, launcher)

    assert session.open("https://www.newsite.com") is context
    assert context.pages[0].urls == ["https://www.newsite.com"]

    session.close()
    assert context.closed is True


def test_write_meta_lands_schema_file(tmp_path):
    """write_meta：公开方法，schema 同设计 §3，不依赖浏览器。"""
    session = make_session(tmp_path, FakeLauncher(None))  # 被调用即炸：不碰浏览器
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
    session = make_session(tmp_path, FakeLauncher(FakeContext([[]])))

    session.forget()
    assert not site_dir.exists()


# ----------------------------------------------------------------------
# 档案锁与异常包装
# ----------------------------------------------------------------------

def test_lock_conflict_raises_when_owner_alive(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        site_dir = tmp_path / "sites" / "taobao"
        site_dir.mkdir(parents=True)
        (site_dir / ".session.lock").write_text(str(proc.pid), encoding="utf-8")
        session = make_session(tmp_path, FakeLauncher(None))

        with pytest.raises(RuntimeError, match="已有会话在跑"):
            session.verify()
        # 报错方从未持有锁，不许动他人活锁：文件原样保留、pid 未被覆盖
        assert (site_dir / ".session.lock").read_text(encoding="utf-8") == str(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_stale_lock_is_taken_over(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # 拿一个确定已死的 PID
    site_dir = tmp_path / "sites" / "taobao"
    site_dir.mkdir(parents=True)
    (site_dir / ".session.lock").write_text(str(proc.pid), encoding="utf-8")

    session = make_session(tmp_path, FakeLauncher(FakeContext([[make_cookie("_tb_token_")]])))
    assert session.verify() is True  # 陈旧锁不挡路，正常接管
    assert not (site_dir / ".session.lock").exists()


def test_launcher_import_error_wrapped_with_install_hint(tmp_path):
    def broken_launcher(user_data_dir, headless):
        raise ImportError("Executable doesn't exist at .../chromium-xxxx")

    session = make_session(tmp_path, broken_launcher)
    with pytest.raises(RuntimeError, match="playwright install"):
        session.verify()
    assert not (tmp_path / "sites" / "taobao" / ".session.lock").exists()
