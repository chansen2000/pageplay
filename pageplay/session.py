"""站点会话：登录、验证、快照刷新、导出、打开驱动、遗忘。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import BrowserContext

from .sites import SitePreset


class SiteSession:
    """一个站点的持久浏览器会话（档案目录 = sites_root/<site.name>/）。"""

    def __init__(self, site: SitePreset, sites_root: Path,
                 launcher: Callable | None = None) -> None:
        """初始化站点会话。

        launcher 可注入（默认 sync_playwright().start().launch_persistent_context），
        测试打桩用——硬契约，不得删。
        """
        raise NotImplementedError

    def login(self, timeout_sec: int = 300) -> bool:
        """headful 打开登录页，每 2s 轮询 cookies。

        登录成功：刷新快照、写 meta.json、返回 True；超时返回 False。
        """
        raise NotImplementedError

    def verify(self) -> bool:
        """headless 起档案读 cookie，判定登录态是否仍有效。"""
        raise NotImplementedError

    def refresh_snapshot(self) -> None:
        """从档案读当前 cookie 并刷新快照文件。"""
        raise NotImplementedError

    def export_cookies(self) -> list[dict]:
        """只读快照导出 cookie 列表，不启动浏览器。"""
        raise NotImplementedError

    def open(self) -> BrowserContext:
        """headful 打开持久 context，供外部驱动。"""
        raise NotImplementedError

    def close(self) -> None:
        """关闭当前会话持有的浏览器资源。"""
        raise NotImplementedError

    def forget(self) -> None:
        """删除站点会话目录（档案 + 快照 + meta），彻底忘记登录态。"""
        raise NotImplementedError
