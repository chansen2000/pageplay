"""站点预设：内置站点表、自定义站点解析、预设枚举。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SitePreset:
    """一个站点的登录/落地/域名/登录态标记配置。"""

    name: str                        # 站点名，如 "taobao"
    login_url: str                   # 登录页 URL
    home_url: str                    # 登录后落地页
    domain: str                      # cookie 归属域，如 "taobao.com"
    check_cookies: tuple[str, ...]   # 登录态标记 cookie 名；空 tuple = 该域有 cookie 即算


_BUILTIN: dict[str, SitePreset] = {
    "taobao": SitePreset(
        name="taobao",
        login_url="https://login.taobao.com/member/login.jhtml",
        home_url="https://www.taobao.com",
        domain="taobao.com",
        check_cookies=("_tb_token_",),
    ),
    "sycm": SitePreset(
        name="sycm",
        login_url="https://login.taobao.com/member/login.jhtml",
        home_url="https://sycm.taobao.com",
        domain="taobao.com",
        check_cookies=("_tb_token_",),
    ),
}


def get_site(name: str) -> SitePreset:
    """按名字查内置站点预设。

    内置表查不到：raise KeyError，消息列出可用站点名并提示 --url 用法。
    """
    raise NotImplementedError


def resolve_site(name: str, login_url: str | None = None) -> SitePreset:
    """解析站点预设。

    login_url 给定时构造自定义预设：domain 取该 URL hostname 的注册域
    （最后两段近似），check_cookies=()，home_url=login_url；
    否则走 get_site 内置表。
    """
    raise NotImplementedError


def list_builtin() -> list[SitePreset]:
    """列出全部内置站点预设。"""
    raise NotImplementedError
