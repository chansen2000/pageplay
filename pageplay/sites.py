"""站点预设：内置站点表、自定义站点解析、预设枚举。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


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
    try:
        return _BUILTIN[name]
    except KeyError:
        available = ", ".join(sorted(_BUILTIN))
        raise KeyError(
            f"未知站点 {name!r}；可用站点：{available}。"
            f"自定义站点请用 --url <登录页URL>，例如 --url https://example.com/login"
        ) from None


def resolve_site(name: str, login_url: str | None = None) -> SitePreset:
    """解析站点预设。

    login_url 给定时构造自定义预设：domain 取该 URL hostname 的注册域
    （最后两段近似），check_cookies=()，home_url=login_url；
    否则走 get_site 内置表。
    """
    if login_url is None:
        return get_site(name)

    hostname = urlsplit(login_url).hostname
    # 单段主机名（如 "localhost"）取不出注册域，同样按非法处理
    if not hostname or "." not in hostname:
        raise ValueError(
            f"无法从 login_url 解析出域名：{login_url!r}；"
            f"请给形如 https://example.com/login 的合法 URL"
        )

    # 近似注册域：取 hostname 最后两段（a.b.example.com → example.com）。
    # 局限：这是朴素近似，处理不了 co.jp / com.cn 这类多段公共后缀
    # （会得到 "co.jp"），也不识别 github.io 等私有后缀；
    # 如需精确注册域要引入 PSL 库（如 publicsuffix2）。
    domain = ".".join(hostname.split(".")[-2:])

    return SitePreset(
        name=name,
        login_url=login_url,
        home_url=login_url,
        domain=domain,
        check_cookies=(),
    )


def parse_target(target: str) -> tuple[str, str | None]:
    """把 login 的目标参数解析成 (站点名, 贴入的URL)。

    - target 含 "://" 或含 "."：视为 URL/域名，无 scheme 补 "https://"；
      站点名 = 注册域（最后两段近似）去掉最后一段（"www.taobao.com"→
      "taobao"，多段公共后缀局限同 resolve_site）；login_url = 补全后的
      完整 URL 原样返回
    - 否则视为站点名，首尾空白剥掉后原样返回，login_url=None
    - 空串/纯空白 raise ValueError；带点但解析不出合法域名同样 raise
      ValueError（下游 resolve_site 对此类 URL 也会拒绝）
    """
    if not target or not target.strip():
        raise ValueError(
            "目标不能为空；请给站点名（如 taobao）或网址/域名（如 www.taobao.com）")
    target = target.strip()
    if "://" not in target and "." not in target:
        return target, None

    url = target if "://" in target else f"https://{target}"
    hostname = urlsplit(url).hostname or ""
    segments = hostname.split(".")
    if "." not in hostname or not all(segments):
        raise ValueError(
            f"无法从目标解析出域名：{target!r}；"
            f"请给形如 www.taobao.com 或 https://example.com/login 的合法目标")
    return segments[-2], url


def list_builtin() -> list[SitePreset]:
    """列出全部内置站点预设。"""
    return list(_BUILTIN.values())
