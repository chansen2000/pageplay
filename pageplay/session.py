"""站点会话：登录、验证、快照刷新、导出、打开驱动、遗忘。

档案锁为 Best-effort：起 context 前在站点目录写 lock 文件（内容为本进程
PID）；属主进程仍存活则明确报错（Playwright 同一 user_data_dir 同时只能
一个实例，不静默等待），属主已死则覆盖接管。它不是跨进程强互斥原语。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from . import cookies
from .sites import SitePreset

log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 2.0
_PROFILE_DIRNAME = "browser-profile"
_LOCK_FILENAME = ".session.lock"
_STATE_FILENAME = "state.json"
_META_FILENAME = "meta.json"


def _pid_alive(pid: int) -> bool:
    """Best-effort 判断进程是否存活（信号 0 探测）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属他人
    except OSError:
        return False
    return True


class SiteSession:
    """一个站点的持久浏览器会话（档案目录 = sites_root/<site.name>/）。"""

    def __init__(self, site: SitePreset, sites_root: Path,
                 launcher: Callable | None = None) -> None:
        """初始化站点会话。

        launcher 可注入（默认 _default_launcher：sync_playwright().start()
        后 launch_persistent_context），测试打桩用——硬契约，不得删。
        """
        self.site = site
        self._sites_root = Path(sites_root)
        self._site_dir = self._sites_root / site.name
        self._profile_dir = self._site_dir / _PROFILE_DIRNAME
        self._lock_path = self._site_dir / _LOCK_FILENAME
        self._launcher: Callable = (
            launcher if launcher is not None else self._default_launcher)
        self._context: BrowserContext | None = None
        self._pw = None           # 默认 launcher 持有的 playwright 驱动
        self._lock_owned = False  # 本实例当前持有档案锁

    # ------------------------------------------------------------------
    # context 启动（唯一入口）与默认 launcher
    # ------------------------------------------------------------------

    def _start_context(self, headless: bool) -> BrowserContext:
        """所有流程起 persistent context 的唯一入口。

        顺序：档案锁检查 → 调 launcher（可注入）→ 异常包装。
        launcher 失败时释放锁，不留陈旧锁挡后续重试。
        """
        if self._context is not None:
            raise RuntimeError(
                f"站点 {self.site.name} 已有活动 context，请先 close()")
        self._acquire_lock()
        try:
            self._context = self._launcher(self._profile_dir, headless)
        except Exception as exc:
            self._release_lock()
            raise self._wrap_launch_error(exc) from exc
        log.info("session %s: context 已启动（headless=%s，profile=%s）",
                 self.site.name, headless, self._profile_dir)
        return self._context

    def _default_launcher(self, user_data_dir: Path, headless: bool) -> BrowserContext:
        """默认启动方式：sync_playwright 起驱动后开 Chromium 持久 context。

        驱动/内核未安装抛出的异常在此包装为带 "playwright install
        chromium" 提示的 RuntimeError（调用点 _wrap_launch_error 识别后
        不二次包装）。
        """
        try:
            self._pw = sync_playwright().start()
            return self._pw.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir), headless=headless)
        except Exception as exc:
            raise RuntimeError(
                f"启动 Chromium 失败：{exc}"
                "（若为首次使用，请先执行 playwright install chromium）") from exc

    @staticmethod
    def _wrap_launch_error(exc: Exception) -> RuntimeError:
        """把 launcher 抛出的任意异常统一包装为带安装提示的 RuntimeError。"""
        if isinstance(exc, RuntimeError) and "playwright install" in str(exc):
            return exc  # _default_launcher 已包装过，不二次包
        return RuntimeError(
            f"启动浏览器失败：{exc}"
            "（若为首次使用，请先执行 playwright install chromium）")

    # ------------------------------------------------------------------
    # 业务动作
    # ------------------------------------------------------------------

    def login(self, timeout_sec: int = 300, url: str | None = None) -> bool:
        """headful 打开登录页，每 2s 轮询 cookies。

        url 给定时打开该页面（login 贴网址命中预设：打开客户贴的页面），
        否则打开预设 login_url，原行为不变。
        登录成功：刷新快照、写 meta.json、返回 True；超时返回 False。
        结束（无论成败）都会关闭浏览器并释放档案锁。
        """
        deadline = time.monotonic() + timeout_sec
        context = self._start_context(headless=False)
        try:
            page = context.new_page()
            page.goto(url if url is not None else self.site.login_url)
            log.info("login %s: 等待人在浏览器完成登录（timeout=%ss）",
                     self.site.name, timeout_sec)
            while True:
                if cookies.has_login_state(context.cookies(),
                                           self.site.domain,
                                           self.site.check_cookies):
                    log.info("login %s: 检测到登录态标记", self.site.name)
                    self.refresh_snapshot()
                    self.write_meta()
                    return True
                if time.monotonic() >= deadline:
                    log.info("login %s: 超时未检测到登录态", self.site.name)
                    return False
                time.sleep(_POLL_INTERVAL_SEC)
        finally:
            self.close()

    def verify(self) -> bool:
        """headless 起档案读 cookie，判定登录态是否仍有效。结束即关闭。"""
        context = self._start_context(headless=True)
        try:
            ok = cookies.has_login_state(context.cookies(),
                                         self.site.domain,
                                         self.site.check_cookies)
        finally:
            self.close()
        log.info("verify %s: 登录态%s", self.site.name, "有效" if ok else "失效")
        return ok

    def refresh_snapshot(self) -> None:
        """从档案读当前 cookie 并刷新快照文件。

        已有活动 context 直接读；否则 headless 起一次、读完即关。
        """
        if self._context is not None:
            cookies.save_snapshot(self._site_dir, self._context.cookies())
            log.info("session %s: 快照已刷新（复用活动 context）", self.site.name)
            return
        context = self._start_context(headless=True)
        try:
            picked = context.cookies()
        finally:
            self.close()
        cookies.save_snapshot(self._site_dir, picked)
        log.info("session %s: 快照已刷新（headless 一次性）", self.site.name)

    def export_cookies(self) -> list[dict]:
        """只读快照导出 cookie 列表，不启动浏览器。"""
        return cookies.load_snapshot(self._site_dir)

    def open(self, url: str | None = None) -> BrowserContext:
        """headful 打开持久 context 并返回，供外部（AI）驱动。

        行为：不轮询；不给 url 时不打开页面、不导航，调用方自行
        context.new_page() / page.goto(...)；给 url 时起 context 后
        new_page().goto(url)（login 贴网址入口用）。用完调 close() 释放。
        """
        context = self._start_context(headless=False)
        if url is not None:
            page = context.new_page()
            page.goto(url)
            log.info("session %s: 已打开 %s", self.site.name, url)
        return context

    def close(self) -> None:
        """关闭当前会话持有的浏览器资源并释放档案锁（幂等）。"""
        context, self._context = self._context, None
        if context is not None:
            try:
                context.close()
            except Exception:
                log.warning("session %s: 关闭 context 失败（忽略）",
                            self.site.name, exc_info=True)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                log.warning("session %s: 停止 playwright 驱动失败（忽略）",
                            self.site.name, exc_info=True)
            self._pw = None
        self._release_lock()

    def forget(self) -> None:
        """删除站点会话目录（档案 + 快照 + meta），彻底忘记登录态。

        目录不存在时抛 FileNotFoundError（ignore_errors=False）。
        """
        self.close()  # 还持有浏览器资源时先释放；他人持锁则由 rmtree 一并清掉
        shutil.rmtree(self._site_dir, ignore_errors=False)
        log.info("session %s: 已删除站点目录 %s", self.site.name, self._site_dir)

    # ------------------------------------------------------------------
    # meta 与档案锁
    # ------------------------------------------------------------------

    def write_meta(self) -> None:
        """写 meta.json（设计 §3 schema：site/login_url/saved_at/check_cookies）。

        login 成功路径与 CLI 交互式登录共用；调用前站点目录必须已存在
        （正常流程 open/login 起档案锁时已建）。
        """
        meta = {
            "site": self.site.name,
            "login_url": self.site.login_url,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "check_cookies": list(self.site.check_cookies),
        }
        path = self._site_dir / _META_FILENAME
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        log.info("session %s: meta 已写入 %s", self.site.name, path)

    def _acquire_lock(self) -> None:
        """起 context 前的 Best-effort 档案锁（类 docstring 见模块头）。

        已有锁且属主进程存活（且非本进程）→ raise RuntimeError；
        属主已死（陈旧锁）或属主为本进程 → 覆盖接管。
        """
        if self._lock_owned:
            return
        self._site_dir.mkdir(parents=True, exist_ok=True)
        if self._lock_path.exists():
            pid = self._read_lock_pid()
            if pid is not None and pid != os.getpid() and _pid_alive(pid):
                raise RuntimeError(
                    f"站点 {self.site.name} 已有会话在跑（pid={pid}），"
                    "同一档案目录同时只能一个浏览器实例；请先关闭已有会话再试")
            log.info("session %s: 接管陈旧/本进程残留锁（lock pid=%s）",
                     self.site.name, pid)
        self._lock_path.write_text(str(os.getpid()), encoding="utf-8")
        self._lock_owned = True

    def _read_lock_pid(self) -> int | None:
        try:
            return int(self._lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _release_lock(self) -> None:
        if not self._lock_owned:
            return
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            log.warning("session %s: 释放档案锁失败（忽略）",
                        self.site.name, exc_info=True)
        self._lock_owned = False
