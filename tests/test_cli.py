"""CLI 单测：只测命令派发与退出码映射，不起真浏览器（真流程归 test_integration）。

浏览器相关路径全部用替身 SiteSession 打桩（monkeypatch pageplay.cli.SiteSession），
这正是因为"不测真浏览器流程"；login 轮询本体已被 test_session 覆盖。
"""

from __future__ import annotations

import json

import pytest

from pageplay.cli import main
from pageplay.guard import RiskTriggered


# ----------------------------------------------------------------------
# SiteSession 替身：记录构造参数，方法行为按 behaviors 配置
# ----------------------------------------------------------------------

def install_stub_session(monkeypatch: pytest.MonkeyPatch, **behaviors):
    """把 cli 里的 SiteSession 换成替身，返回"已创建实例"列表供断言。"""
    import pageplay.cli as cli

    created: list = []

    class StubSession:
        def __init__(self, site, sites_root):
            self.site = site
            self.sites_root = sites_root
            created.append(self)

        def login(self, timeout_sec: int = 300) -> bool:
            self.login_timeout = timeout_sec
            if isinstance(behaviors.get("login"), Exception):
                raise behaviors["login"]
            return behaviors.get("login", True)

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
    assert session.login_timeout == 0     # 极小超时直达超时路径
    err = capsys.readouterr().err
    assert "超时" in err and "login" in err  # 人话提示重跑 login


def test_login_success_returns_zero_and_next_commands(home_dir, monkeypatch, capsys):
    install_stub_session(monkeypatch, login=True)

    assert main(["login", "taobao"]) == 0
    out = capsys.readouterr().out
    assert "taobao" in out and "登录成功" in out
    assert "doctor taobao" in out and "export taobao" in out  # 后续命令指引


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
# 退出码映射：风控 2 / 非法站点名 1
# ----------------------------------------------------------------------

def test_risk_triggered_maps_to_two(home_dir, monkeypatch, capsys):
    install_stub_session(monkeypatch, verify=RiskTriggered("响应命中风控关键词：验证码"))

    assert main(["doctor", "taobao"]) == 2
    err = capsys.readouterr().err
    assert "风控" in err


def test_unknown_site_name_returns_one(home_dir, capsys):
    assert main(["doctor", "no-such-site"]) == 1
    err = capsys.readouterr().err
    assert "未知站点" in err  # get_site KeyError 消息含可用站点提示
