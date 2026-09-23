"""sites.py 单测：内置预设字段、get_site 报错消息、resolve_site 域名推导、frozen 不可变。无需网络。"""

from __future__ import annotations

import dataclasses

import pytest

from pageplay.sites import SitePreset, get_site, list_builtin, resolve_site


class TestBuiltinPresets:
    """内置表字段逐一断言（taobao/sycm 全字段）。"""

    def test_taobao_all_fields(self) -> None:
        site = get_site("taobao")
        assert site.name == "taobao"
        assert site.login_url == "https://login.taobao.com/member/login.jhtml"
        assert site.home_url == "https://www.taobao.com"
        assert site.domain == "taobao.com"
        assert site.check_cookies == ("_tb_token_",)

    def test_sycm_all_fields(self) -> None:
        site = get_site("sycm")
        assert site.name == "sycm"
        assert site.login_url == "https://login.taobao.com/member/login.jhtml"
        assert site.home_url == "https://sycm.taobao.com"
        assert site.domain == "taobao.com"
        assert site.check_cookies == ("_tb_token_",)

    def test_list_builtin_returns_all(self) -> None:
        sites = list_builtin()
        assert {s.name for s in sites} == {"taobao", "sycm"}
        assert all(isinstance(s, SitePreset) for s in sites)


class TestGetSite:
    def test_unknown_name_keyerror_message(self) -> None:
        """未知名 raise KeyError，消息列出可用站点名并提示 --url 用法。"""
        with pytest.raises(KeyError, match="taobao") as excinfo:
            get_site("jd")
        assert "--url" in str(excinfo.value)


class TestResolveSite:
    def test_none_with_builtin_name_returns_builtin(self) -> None:
        assert resolve_site("taobao") is get_site("taobao")
        assert resolve_site("sycm") is get_site("sycm")

    def test_none_with_unknown_name_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="--url"):
            resolve_site("jd")

    def test_custom_url_three_segment_hostname(self) -> None:
        site = resolve_site("mysite", "https://a.b.example.com/login")
        assert site.name == "mysite"  # name 保留传入值
        assert site.login_url == "https://a.b.example.com/login"
        assert site.home_url == "https://a.b.example.com/login"
        assert site.domain == "example.com"
        assert site.check_cookies == ()

    def test_custom_url_two_segment_hostname(self) -> None:
        site = resolve_site("mysite", "https://example.com/login")
        assert site.domain == "example.com"
        assert site.check_cookies == ()

    def test_custom_url_empty_check_cookies_is_tuple(self) -> None:
        site = resolve_site("mysite", "https://example.com/login")
        assert isinstance(site.check_cookies, tuple)
        assert len(site.check_cookies) == 0

    def test_invalid_url_raises_valueerror(self) -> None:
        for bad in ("not a url", "https://"):
            with pytest.raises(ValueError):
                resolve_site("mysite", bad)


class TestFrozen:
    def test_site_preset_is_frozen(self) -> None:
        site = get_site("taobao")
        with pytest.raises(dataclasses.FrozenInstanceError):
            site.name = "evil"  # type: ignore[misc]
