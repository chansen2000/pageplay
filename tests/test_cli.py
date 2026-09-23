"""CLI 单测：只测命令派发与退出码映射，不起真浏览器（真流程归 test_integration）。

浏览器相关路径全部用替身 SiteSession 打桩（monkeypatch pageplay.cli.SiteSession），
这正是因为"不测真浏览器流程"；login 轮询本体已被 test_session 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pageplay.cli import main
from pageplay.guard import RiskTriggered
from pageplay.sites import get_site


# ----------------------------------------------------------------------
# SiteSession 替身：记录构造参数，方法行为按 behaviors 配置
# ----------------------------------------------------------------------

class StubPage:
    """替身页面：只记 goto 的 URL。"""

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    def goto(self, url: str) -> None:
        self._urls.append(url)


class StubContext:
    """替身 context：cookies() 逐轮返回 batches，最后一批永久重复。"""

    def __init__(self, cookie_batches: list[list[dict]]) -> None:
        self._batches = list(cookie_batches) or [[]]
        self.goto_urls: list[str] = []
        self.closed = False

    def cookies(self) -> list[dict]:
        if len(self._batches) > 1:
            return self._batches.pop(0)
        return self._batches[0]

    def new_page(self) -> StubPage:
        return StubPage(self.goto_urls)

    def close(self) -> None:
        self.closed = True


def install_stub_session(monkeypatch: pytest.MonkeyPatch, **behaviors):
    """把 cli 里的 SiteSession 换成替身，返回"已创建实例"列表供断言。"""
    import pageplay.cli as cli

    from pageplay.cookies import save_snapshot

    created: list = []
    context = behaviors.get("context")

    class StubSession:
        def __init__(self, site, sites_root):
            self.site = site
            self.sites_root = sites_root
            self.login_calls: list[dict] = []
            self.opened_urls: list[str] = []
            self.snapshot_refreshed = False
            self.meta_written = False
            self.closed = False
            created.append(self)

        def login(self, timeout_sec: int = 300, url: str | None = None) -> bool:
            self.login_calls.append({"timeout_sec": timeout_sec, "url": url})
            if isinstance(behaviors.get("login"), Exception):
                raise behaviors["login"]
            return behaviors.get("login", True)

        def open(self, url: str | None = None):
            self.opened_urls.append(url)
            assert context is not None, "open 被意外调用（未注入 context）"
            return context

        def refresh_snapshot(self) -> None:
            self.snapshot_refreshed = True
            save_snapshot(self.sites_root / self.site.name, context.cookies())

        def write_meta(self) -> None:
            self.meta_written = True

        def close(self) -> None:
            self.closed = True

        def verify(self) -> bool:
            if isinstance(behaviors.get("verify"), Exception):
                raise behaviors["verify"]
            return behaviors.get("verify", True)

        def forget(self) -> None:
            if isinstance(behaviors.get("forget"), Exception):
                raise behaviors["forget"]

    monkeypatch.setattr(cli, "SiteSession", StubSession)
    return created


# ----------------------------------------------------------------------
# list
# ----------------------------------------------------------------------

def test_list_empty_home_returns_zero(home_dir, capsys):
    home_dir.mkdir(parents=True)
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "taobao" in out and "sycm" in out  # 内置预设仍列出
    assert "未登录" in out


def test_list_shows_saved_custom_site(home_dir, capsys):
    site_dir = home_dir / "sites" / "faketest"
    site_dir.mkdir(parents=True)
    (site_dir / "meta.json").write_text(json.dumps({
        "site": "faketest", "login_url": "https://demo.example.com/login",
        "saved_at": "2026-09-23T10:00:00", "check_cookies": ["sessionid"],
    }), encoding="utf-8")
    (site_dir / "state.json").write_text(json.dumps({
        "version": 1, "site": "faketest",
        "saved_at": "2026-09-23T10:00:00", "cookies": [],
    }), encoding="utf-8")

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "faketest" in out
    assert "2026-09-23T10:00:00" in out  # 打印快照时间而非"未登录"


# ----------------------------------------------------------------------
# login（替身，不真开浏览器）
# ----------------------------------------------------------------------

def test_login_timeout_returns_one(home_dir, fake_site, monkeypatch, capsys):
    created = install_stub_session(monkeypatch, login=False)
    url = fake_site + "/login"

    rc = main(["login", "mysite", "--url", url, "--timeout", "0"])

    assert rc == 1
    (session,) = created
    assert session.site.name == "mysite"
    assert session.site.login_url == url  # --url 已传入站点解析
    assert session.login_calls[0]["timeout_sec"] == 0  # 极小超时直达超时路径
    err = capsys.readouterr().err
    assert "超时" in err and "login" in err  # 人话提示重跑 login


def test_login_success_returns_zero_and_next_commands(home_dir, monkeypatch, capsys):
    install_stub_session(monkeypatch, login=True)

    assert main(["login", "taobao"]) == 0
    out = capsys.readouterr().out
    assert "taobao" in out and "登录成功" in out
    assert "doctor taobao" in out and "export taobao" in out  # 后续命令指引


# ----------------------------------------------------------------------
# login 贴网址/域名：自动档（命中预设）与回车档（陌生站）
# ----------------------------------------------------------------------

def _enter_cookie(domain: str = "newsite.com") -> dict:
    return {"name": "sid", "value": "v1", "domain": domain, "path": "/",
            "expires": -1, "httpOnly": False, "secure": False}


def test_login_pasted_builtin_url_keeps_preset_polling(home_dir, monkeypatch, capsys):
    """贴 www.taobao.com：命中 taobao 预设（标记保留），打开贴的页面。"""
    created = install_stub_session(monkeypatch, login=True)

    assert main(["login", "www.taobao.com"]) == 0

    (session,) = created
    assert session.site.name == "taobao"
    assert session.site.check_cookies == ("_tb_token_",)   # 预设 check_cookies 沿用
    assert session.login_calls == [
        {"timeout_sec": 300, "url": "https://www.taobao.com"}]  # 自动档打开贴的 URL
    assert session.opened_urls == []  # 未走回车档 open
    out = capsys.readouterr().out
    assert "登录成功" in out


def test_login_preset_name_still_auto_without_url(home_dir, monkeypatch, capsys):
    """预设名兼容：login taobao 走原自动档，url=None（用预设 login_url）。"""
    created = install_stub_session(monkeypatch, login=True)

    assert main(["login", "taobao"]) == 0
    (session,) = created
    assert session.login_calls == [{"timeout_sec": 300, "url": None}]
    assert session.site.login_url == get_site("taobao").login_url


def test_login_pasted_unknown_site_enter_mode_full_flow(home_dir, monkeypatch, capsys):
    """回车档全流程：空 cookie 提示再按 → 第二次抓到 → 快照/meta 落地 → 0。"""
    created = install_stub_session(monkeypatch, context=StubContext([
        [], [_enter_cookie()],
    ]))
    presses = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda: next(presses))

    rc = main(["login", "www.newsite.com"])

    assert rc == 0
    (session,) = created
    assert session.site.name == "newsite"          # 注册域推导的站点名
    assert session.site.domain == "newsite.com"
    assert session.site.check_cookies == ()        # 陌生站：有 cookie 即算
    assert session.opened_urls == ["https://www.newsite.com"]  # 打开贴的页面
    assert session.snapshot_refreshed and session.meta_written
    assert session.closed is True                  # 结束必关浏览器
    out = capsys.readouterr().out
    assert "https://www.newsite.com" in out
    assert "按回车" in out
    assert "未抓到该域 cookie" in out              # 第一次空 cookie 的提示
    assert "登录成功" in out and "doctor newsite" in out
    state = json.loads(
        (home_dir / "sites" / "newsite" / "state.json").read_text(encoding="utf-8"))
    assert state["cookies"] == [_enter_cookie()]   # 快照真实落地


def test_login_pasted_unknown_site_first_press_has_cookie(home_dir, monkeypatch, capsys):
    """一次回车即抓到 cookie：不提示"未抓到"，直接保存。"""
    created = install_stub_session(monkeypatch, context=StubContext([
        [_enter_cookie()],
    ]))
    presses = iter([""])
    monkeypatch.setattr("builtins.input", lambda: next(presses))

    assert main(["login", "newsite.com"]) == 0     # 裸域名同样可贴
    (session,) = created
    assert session.opened_urls == ["https://newsite.com"]
    out = capsys.readouterr().out
    assert "未抓到该域 cookie" not in out


def test_login_enter_mode_cookie_of_other_domain_keeps_waiting(home_dir, monkeypatch, capsys):
    """回车档按域过滤：只有他域 cookie 也算没抓到，继续等回车。"""
    created = install_stub_session(monkeypatch, context=StubContext([
        [_enter_cookie(domain="other.com")], [_enter_cookie()],
    ]))
    presses = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda: next(presses))

    assert main(["login", "www.newsite.com"]) == 0
    out = capsys.readouterr().out
    assert "未抓到该域 cookie" in out              # 他域 cookie 不通过


def test_login_enter_mode_interrupt_returns_130_and_closes(home_dir, monkeypatch, capsys):
    """回车等待中 Ctrl-C：130 退出且浏览器已关（不漏档案锁）。"""
    created = install_stub_session(monkeypatch, context=StubContext([[]]))

    def raise_interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_interrupt)

    assert main(["login", "www.newsite.com"]) == 130
    (session,) = created
    assert session.closed is True
    err = capsys.readouterr().err
    assert "已中断" in err


# ----------------------------------------------------------------------
# export / forget（走真实 SiteSession：不触浏览器，只碰快照/目录）
# ----------------------------------------------------------------------

def test_export_without_snapshot_returns_one_with_login_hint(home_dir, capsys):
    assert main(["export", "taobao"]) == 1
    err = capsys.readouterr().err
    assert "login" in err  # FileNotFoundError 消息自带"先 pageplay login"指引


def test_forget_missing_site_returns_one(home_dir, capsys):
    assert main(["forget", "taobao"]) == 1
    err = capsys.readouterr().err
    assert "无需删除" in err


# ----------------------------------------------------------------------
# 非 login 命令贴网址/域名：统一解析（T5：与 login 同一套推导）
# ----------------------------------------------------------------------

def _write_saved_site(home_dir: Path, name: str = "faketest",
                      login_url: str = "https://demo.example.com/login") -> Path:
    """手写一个已登录站点现场（meta.json + state.json），等价 login 落盘。"""
    site_dir = home_dir / "sites" / name
    site_dir.mkdir(parents=True)
    (site_dir / "meta.json").write_text(json.dumps({
        "site": name, "login_url": login_url,
        "saved_at": "2026-09-23T10:00:00", "check_cookies": ["sessionid"],
    }), encoding="utf-8")
    (site_dir / "state.json").write_text(json.dumps({
        "version": 1, "site": name, "saved_at": "2026-09-23T10:00:00",
        "cookies": [{"name": "sessionid", "value": "sess-v1",
                     "domain": "demo.example.com", "path": "/", "expires": -1,
                     "httpOnly": False, "secure": False}],
    }), encoding="utf-8")
    return site_dir


def test_doctor_pasted_url_of_saved_site_resolves_and_verifies(
        home_dir, monkeypatch, capsys):
    """贴 URL 形式访问已存站：faketest.xxx → 推导 faketest → 命中已存。"""
    _write_saved_site(home_dir)
    created = install_stub_session(
        monkeypatch, verify=True, context=StubContext([[]]))

    assert main(["doctor", "faketest.xxx"]) == 0

    (session,) = created
    assert session.site.name == "faketest"
    assert session.site.login_url == "https://demo.example.com/login"
    assert session.site.domain == "example.com"   # meta login_url 推导注册域
    assert session.site.check_cookies == ("sessionid",)
    assert session.snapshot_refreshed is True
    out = capsys.readouterr().out
    assert "faketest" in out and "登录态有效" in out


def test_export_pasted_url_of_saved_site_exports_cookies(home_dir, capsys):
    """export 同样贴 URL：真实 SiteSession 只读快照，导出含种下的 cookie。"""
    _write_saved_site(home_dir)

    assert main(["export", "faketest.xxx"]) == 0

    out = capsys.readouterr().out
    assert "sessionid" in out and "sess-v1" in out


def test_saved_meta_takes_priority_over_builtin(home_dir, monkeypatch):
    """已存优先：taobao 有登录记录时按 meta 重建预设，不用内置表。"""
    _write_saved_site(home_dir, name="taobao",
                      login_url="https://custom.example.com/login")
    created = install_stub_session(
        monkeypatch, verify=True, context=StubContext([[]]))

    assert main(["doctor", "www.taobao.com"]) == 0

    (session,) = created
    assert session.site.login_url == "https://custom.example.com/login"
    assert session.site.check_cookies == ("sessionid",)  # 非内置的 _tb_token_


def test_doctor_pasted_builtin_url_unsaved_resolves_builtin(home_dir, monkeypatch):
    """贴内置站 URL 但从未登录：推导名命中内置预设，验活照常走。"""
    created = install_stub_session(
        monkeypatch, verify=True, context=StubContext([[]]))

    assert main(["doctor", "www.taobao.com"]) == 0

    (session,) = created
    assert session.site.name == "taobao"
    assert session.site.check_cookies == ("_tb_token_",)


def test_doctor_preset_name_still_resolves_builtin(home_dir, monkeypatch, capsys):
    """预设名不回归：doctor taobao 照常解析内置预设并验活。"""
    created = install_stub_session(
        monkeypatch, verify=True, context=StubContext([[]]))

    assert main(["doctor", "taobao"]) == 0

    (session,) = created
    assert session.site.name == "taobao"
    assert "登录态有效" in capsys.readouterr().out


def test_pasted_unknown_site_without_login_returns_one_with_login_hint(
        home_dir, capsys):
    """贴从未登录过的 URL：报错含 login 指引与已存站点清单，无 --url 误导。"""
    _write_saved_site(home_dir)  # 让"已存站点"列表有内容可断言

    assert main(["doctor", "neverlogin.com"]) == 1

    err = capsys.readouterr().err
    assert "还没有登录记录" in err
    assert "pageplay login neverlogin.com" in err  # 原样回显输入作指引
    assert "已存站点：faketest" in err
    assert "内置：sycm, taobao" in err
    assert "--url" not in err  # 误导提示已删


def test_forget_pasted_url_of_saved_site(home_dir, capsys):
    """forget 同一入口：贴 URL 推导名字命中已存，目录被删。"""
    site_dir = _write_saved_site(home_dir)

    assert main(["forget", "faketest.xxx"]) == 0

    assert not site_dir.exists()
    assert "已删除" in capsys.readouterr().out


# ----------------------------------------------------------------------
# 退出码映射：风控 2 / 未保存且未内置的站点名 1
# ----------------------------------------------------------------------

def test_risk_triggered_maps_to_two(home_dir, monkeypatch, capsys):
    install_stub_session(monkeypatch, verify=RiskTriggered("响应命中风控关键词：验证码"))

    assert main(["doctor", "taobao"]) == 2
    err = capsys.readouterr().err
    assert "风控" in err


def test_unsaved_site_name_returns_one_with_login_hint(home_dir, capsys):
    """未保存也未内置的名字：报错给 login 指引与可用站点清单。"""
    assert main(["doctor", "no-such-site"]) == 1
    err = capsys.readouterr().err
    assert "还没有登录记录" in err
    assert "pageplay login no-such-site" in err
    assert "已存站点" in err and "内置：sycm, taobao" in err
