"""sites.py 单测：内置预设字段、get_site 报错消息、resolve_site 域名推导、parse_target 分支、frozen 不可变。无需网络。"""

from __future__ import annotations

import dataclasses

import pytest

from pageplay.sites import SitePreset, get_site, list_builtin, parse_target, resolve_site


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


class TestParseTarget:
    """login 目标参数解析：贴 URL/域名两档 vs 站点名原样。"""

    def test_domain_with_www_derives_registered_name(self) -> None:
        assert parse_target("www.taobao.com") == ("taobao", "https://www.taobao.com")

    def test_bare_domain_two_segments(self) -> None:
        assert parse_target("taobao.com") == ("taobao", "https://taobao.com")

    def test_subdomain_hits_same_registered_domain(self) -> None:
        # 注册域近似：sycm.taobao.com → taobao（与 resolve_site 局限一致）
        assert parse_target("sycm.taobao.com") == ("taobao", "https://sycm.taobao.com")

    def test_url_with_scheme_and_path_kept_as_is(self) -> None:
        url = "https://login.taobao.com/member/login.jhtml"
        assert parse_target(url) == ("taobao", url)

    def test_multisegment_host_takes_second_to_last(self) -> None:
        assert parse_target("a.b.example.com") == ("example", "https://a.b.example.com")

    def test_preset_name_returned_as_is(self) -> None:
        assert parse_target("taobao") == ("taobao", None)
        assert parse_target(" mysite ") == ("mysite", None)  # 首尾空白剥掉

    def test_empty_or_blank_raises_valueerror(self) -> None:
        for bad in ("", "   "):
            with pytest.raises(ValueError):
                parse_target(bad)

    def test_dotted_but_invalid_target_raises_valueerror(self) -> None:
        with pytest.raises(ValueError):
            parse_target("https://")
    def test_site_preset_is_frozen(self) -> None:
        site = get_site("taobao")
        with pytest.raises(dataclasses.FrozenInstanceError):
            site.name = "evil"  # type: ignore[misc]
