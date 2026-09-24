"""T10 常驻守护测试：session.json 簿记、CDP 往返、关停；风控协作函数。

守护往返用真 Chromium（无内核 pytest.skip，不假绿）；簿记与协作语义用
替身钉死。真人过验证的模拟沿用本目录惯例（sync API 禁跨线程，不另起
线程碰 playwright）：假站页面自带延时脚本 fetch /pass，服务端对 /pass
回 Set-Cookie——轮询期间登录标记"自己出现"，与真人滑块通过后 cookie
落袋同一可观测量（详见 conftest.collab_site）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from pageplay import session  # noqa: E402
from pageplay.cli import main  # noqa: E402
from pageplay.recipes import save_recipe  # noqa: E402
from pageplay.runs import load_runs  # noqa: E402
from pageplay.cli_pick import _runs_path  # noqa: E402
from pageplay.sites import SitePreset  # noqa: E402


def make_cookie(name: str, domain: str) -> dict:
    return {"name": name, "value": "v-" + name, "domain": domain, "path": "/",
            "expires": -1, "httpOnly": False, "secure": False}


COLLAB_SITE = SitePreset(
    name="demo", login_url="http://demo.example.com/login",
    home_url="http://demo.example.com/home", domain="demo.com",
    check_cookies=("sid",),
)


class FakePage:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    def goto(self, url: str) -> None:
        self.urls.append(url)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """cookies() 逐轮返回 batches，最后一批永久重复。"""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches) or [[]]
        self.pages: list[FakePage] = []

    def cookies(self) -> list[dict]:
        if len(self._batches) > 1:
            return self._batches.pop(0)
        return self._batches[0]

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]


@pytest.fixture
def daemon(home_dir):
    """真 Chromium 守护夹具：内核可用才跑；结束兜底 shutdown 不留进程。"""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
            if not (exe and Path(exe).exists()):
                raise RuntimeError(f"内核缺失：{exe}")
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过守护测试：{exc}")
    yield home_dir
    session.shutdown_browser()
    # 生产 CLI 进程退出即带走了驱动；测试进程同线程复用会撞上仍活的
    # asyncio loop（"Sync API inside asyncio loop"），此处显式收掉并清单例
    driver, session._PW = session._PW, None
    if driver is not None:
        try:
            driver.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 守护往返（真 Chromium）
# ----------------------------------------------------------------------

def test_daemon_round_trip_spawn_attach_pageshut_shutdown(daemon):
    """起守护 → session.json 在 → 再 ensure 附着同一 pid → 关页浏览器仍活
    → shutdown 杀掉 → 文件清掉 → 再 shutdown 报没有。"""
    browser = session.ensure_browser(headless=True)
    info = json.loads((daemon / "session.json").read_text(encoding="utf-8"))
    assert info["headless"] is True
    assert info["pid"] > 0 and info["port"] > 0
    assert session._cdp_alive(info["port"])

    browser2 = session.ensure_browser(headless=False)  # 幂等：附着同一守护
    info2 = json.loads((daemon / "session.json").read_text(encoding="utf-8"))
    assert info2["pid"] == info["pid"] and info2["port"] == info["port"]

    context = browser2.contexts[0]                     # CDP 默认上下文
    page = context.new_page()
    page.goto("about:blank")
    page.close()                                       # close_soft：关页不关浏览器
    assert session._cdp_alive(info["port"])            # 浏览器仍活
    assert browser.contexts                            # 连接仍可用

    assert session.shutdown_browser() is True
    assert not (daemon / "session.json").exists()
    deadline = time.monotonic() + 10
    while session._cdp_alive(info["port"]) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not session._cdp_alive(info["port"])        # 守护确实死了
    assert session.shutdown_browser() is False         # 没有守护可关


def test_daemon_stale_session_json_cleaned_and_respawned(daemon):
    """死 pid + 死端口的陈旧 session.json：验活判死、文件清理、起新守护。"""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()  # 拿一个确定已死的 PID
    daemon.mkdir(parents=True, exist_ok=True)
    stale = {"port": 59999, "pid": dead.pid, "headless": False}
    (daemon / "session.json").write_text(json.dumps(stale), encoding="utf-8")

    assert session.daemon_info() is None               # 验活判死
    assert not (daemon / "session.json").exists()      # 残留已清

    browser = session.ensure_browser(headless=True)    # 照常起新守护
    info = json.loads((daemon / "session.json").read_text(encoding="utf-8"))
    assert info["pid"] != dead.pid and info["headless"] is True
    assert browser.contexts


def test_daemon_corrupt_session_json_cleaned(daemon):
    daemon.mkdir(parents=True, exist_ok=True)
    (daemon / "session.json").write_text("不是json", encoding="utf-8")
    assert session.daemon_info() is None
    assert not (daemon / "session.json").exists()


# ----------------------------------------------------------------------
# 簿记与起守护替身单测（不碰真浏览器）
# ----------------------------------------------------------------------

def test_ensure_browser_missing_executable_gives_install_hint(home_dir, monkeypatch):
    class FakeChromium:
        executable_path = "/nonexistent/chromium-0000"

    class FakeDriver:
        chromium = FakeChromium()

    monkeypatch.setattr(session, "_driver", lambda: FakeDriver())
    with pytest.raises(RuntimeError, match="playwright install"):
        session.ensure_browser()
    assert not (home_dir / "session.json").exists()


def test_ensure_headful_restarts_headless_daemon(monkeypatch):
    """无头守护在场：先 shutdown 再起有头（人工交互必须有可见窗口）。"""
    calls: list = []
    monkeypatch.setattr(session, "daemon_info",
                        lambda: {"port": 1, "pid": 2, "headless": True})
    monkeypatch.setattr(session, "shutdown_browser",
                        lambda: calls.append("shutdown") or True)

    def fake_ensure(headless: bool = False):
        calls.append(f"ensure:{headless}")
        return f"browser-{headless}"

    monkeypatch.setattr(session, "ensure_browser", fake_ensure)
    assert session.ensure_headful_browser() == "browser-False"
    assert calls == ["shutdown", "ensure:False"]


def test_ensure_headful_attaches_headful_daemon(monkeypatch):
    monkeypatch.setattr(session, "daemon_info",
                        lambda: {"port": 1, "pid": 2, "headless": False})

    def boom():
        raise AssertionError("有头守护在场不该 shutdown")

    monkeypatch.setattr(session, "shutdown_browser", boom)
    monkeypatch.setattr(session, "ensure_browser",
                        lambda headless=False: "attached")
    assert session.ensure_headful_browser() == "attached"


def test_shutdown_without_daemon_returns_false(home_dir, monkeypatch):
    monkeypatch.setattr(session, "daemon_info", lambda: None)
    assert session.shutdown_browser() is False


# ----------------------------------------------------------------------
# ensure_logged_in（风控人机协作语义）
# ----------------------------------------------------------------------

def test_collab_sees_marker_after_human_passes(monkeypatch, capsys):
    """轮询期间登录标记出现（= 人过了验证）→ True；开的是落地页，完事关页。"""
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    monkeypatch.setattr(session, "daemon_info",
                        lambda: {"port": 1, "pid": 2, "headless": False})
    context = FakeContext([[], [make_cookie("sid", "demo.com")]])
    assert session.ensure_logged_in(FakeBrowser(context), COLLAB_SITE,
                                    max_wait=60) is True
    out = capsys.readouterr().out
    assert "请在浏览器窗口完成登录/验证" in out
    assert context.pages[0].urls == ["http://demo.example.com/home"]
    assert context.pages[0].closed is True


def test_collab_timeout_returns_false(capsys, monkeypatch):
    """等满 max_wait 没人过 → False + 人话；页面照收。"""
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    monkeypatch.setattr(session, "daemon_info",
                        lambda: {"port": 1, "pid": 2, "headless": False})
    context = FakeContext([[]])
    assert session.ensure_logged_in(FakeBrowser(context), COLLAB_SITE,
                                    max_wait=0) is False
    out = capsys.readouterr().out
    assert "超时" in out and "请在浏览器窗口完成登录/验证" in out
    assert context.pages[0].closed is True


def test_collab_headless_daemon_fails_fast_with_hint(capsys, monkeypatch):
    """无头守护人无法介入：提示改有头重跑并直接失败（不碰浏览器）。"""
    monkeypatch.setattr(session, "daemon_info",
                        lambda: {"port": 1, "pid": 2, "headless": True})
    # browser=None：真走到开页就会炸——失败必须发生在碰浏览器之前
    assert session.ensure_logged_in(None, COLLAB_SITE) is False
    assert "无头" in capsys.readouterr().out


# ----------------------------------------------------------------------
# 端到端：run 被弹回登录页 → 人过验证 → 自动续跑（真守护 + 假站）
# ----------------------------------------------------------------------

def _seed_collab_site(home: Path, neutral_base: str) -> None:
    """种协作假站现场：meta 域名/首页与 recipe 都挂中性主机。

    反弹不是 recipe 的锅，是假站的：/table 无 cookie 时 302 到
    login.localhost 的 /login（落点 host 含 "login." → run 反弹判定
    命中）。meta login_url = 中性 /home（域名取 "127.0.0.1" 后两段
    近似，与 /pass 种下的 host-only cookie 域一致）。
    """
    site_dir = home / "sites" / "collabdemo"
    site_dir.mkdir(parents=True)
    (site_dir / "meta.json").write_text(json.dumps({
        "site": "collabdemo",
        "login_url": neutral_base + "/home",
        "saved_at": "2026-09-24T00:00:00",
        "check_cookies": ["sessionid"],
    }, ensure_ascii=False), encoding="utf-8")
    save_recipe(site_dir, {
        "version": 1, "name": "collab-1", "site": "collabdemo",
        "url": neutral_base + "/table", "action": "table",
        "selector": "#data", "columns": None,
    })


def test_run_login_bounce_recovers_after_human_passes(
        daemon, collab_site, tmp_path, monkeypatch, capsys):
    """run 直奔订单页被弹回登录页 → 协作等人 → 假站延时种 cookie（=人过）
    → 自动续跑抓表落盘。全链走 cli.main 真入口。"""
    _seed_collab_site(daemon, collab_site)  # recipe/首页都在中性主机
    out_dir = tmp_path / "collab-out"
    monkeypatch.setattr("pageplay.session.time.sleep", lambda _s: None)
    # 系统代理变量（http_proxy/ALL_PROXY 等）会劫持 connect_over_cdp 的
    # 本机回环请求：_cdp_alive 走 _OPENER 直连拿到 200，playwright driver
    # 却遵循代理变量经代理转发 → 503（2026-09-24 实测，含/不含代理变量
    # 对照 1 skipped+泄漏 vs 11 passed）。且该异常发生在 chromium 已起、
    # session.json 未写之间，会漏守护进程放大并发下的资源争抢。本测试在
    # 起 driver 前摘掉代理变量，CDP 往返即与生产无代理路径一致。
    for _var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(_var, False)  # raise 是关键字，只能按位置传
    browser = None
    last: Exception | None = None
    for _attempt in range(3):  # 守护冷启动/连接瞬时抖动：放宽重试，全败才跳过
        try:
            browser = session.ensure_browser(headless=False)  # 有头（无显示如实跳过）
            break
        except Exception as exc:
            last, browser = exc, None
    if browser is None:
        pytest.skip(f"无法起有头守护：{last}")

    assert main(["run", "collab-1", "--out", str(out_dir)]) == 0

    out = capsys.readouterr().out
    assert "请在浏览器窗口完成登录/验证" in out   # 进了风控协作
    assert "✓" in out and "已生成" in out          # 人过后自动续跑成功
    rows = json.loads(
        list(out_dir.glob("collab-1-*.json"))[0].read_text(encoding="utf-8"))
    assert len(rows) == 2 and rows[0]["名称"] == "商品1"
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "ok" and len(entry["outputs"]) == 2
