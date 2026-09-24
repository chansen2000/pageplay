"""站点会话与常驻浏览器守护：登录、验证、快照刷新、导出、打开、遗忘。

T10 常驻模型（设计 §11，sycm-cli CDP 血统）：浏览器是**脱离父进程的
守护进程**（session.json 记录 调试端口+PID+headless），Playwright 只经
connect_over_cdp 附着干活。命令结束只关自己开的页（page.close()），
绝不调 browser/context.close()——人关窗（有头）或 pageplay shutdown
（无头守护）是唯一退出。所有站点共享 <PAGEPLAY_HOME>/browser-profile
一个档案（cookie 本就按域隔离）；state.json/meta.json/recipes/runs
语义不变；旧 sites/<site>/browser-profile 不再读写也不删。

风控人机协作 ensure_logged_in：自动化被弹回登录页时，开站点首页、
终端提示人在窗口里完成登录/验证，每 2s 轮询登录标记（至多 120s），
通过即自动续跑原流程——半自动业务，会话生命周期归人。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, sync_playwright

from . import cookies
from .sites import SitePreset

log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 2.0
_WARM_POLL_INTERVAL_SEC = 1.0   # verify 暖检查轮询间隔
_WARM_POLL_MAX_TRIES = 15       # verify 暖检查至多等 15s（每 1s 一查）
_STATE_FILENAME = "state.json"
_META_FILENAME = "meta.json"

# ----------------------------------------------------------------------
# 常驻浏览器守护（session.json 簿记 + CDP 附着）
# ----------------------------------------------------------------------

_SESSION_FILENAME = "session.json"
_PROFILE_DIRNAME = "browser-profile"   # 共享档案：<home>/browser-profile
_DAEMON_WAIT_SEC = 30.0                # 起守护后等调试端口就绪的上限
_COLLAB_MAX_WAIT_SEC = 120             # 风控协作等人的默认上限

# 直连本机回环：系统代理（http_proxy 等）不许劫持 127.0.0.1 的 CDP 探活
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def _home_dir() -> Path:
    """PAGEPLAY_HOME 覆盖，默认 ~/.pageplay（session.json 与共享档案的家）。"""
    return Path(os.environ.get("PAGEPLAY_HOME", "~/.pageplay")).expanduser()


def _session_file() -> Path:
    return _home_dir() / _SESSION_FILENAME


def _cdp_alive(port: int) -> bool:
    """调试端口探活：GET /json/version 通即活（网络/端口错误一律判死）。"""
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/json/version",
                          timeout=1.5) as resp:
            return resp.status == 200
    except OSError:
        return False


def _free_port() -> int:
    """向内核要一个当前空闲的回环端口（起守护瞬间被占的竞态可忽略）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def daemon_info() -> dict | None:
    """读 session.json 并验活：活守护返回 {"port","pid","headless"}。

    pid 已死、调试端口无响应、文件缺失或损坏 → 清理残留（文件删掉）
    并返回 None。这是"附着 or 起守护"判定的唯一入口。
    """
    path = _session_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        info = {"port": int(data["port"]), "pid": int(data["pid"]),
                "headless": bool(data.get("headless", False))}
    except (OSError, ValueError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return None
    if not _pid_alive(info["pid"]) or not _cdp_alive(info["port"]):
        log.info("daemon: session.json 指向的守护已死（pid=%s port=%s），清理",
                 info["pid"], info["port"])
        path.unlink(missing_ok=True)
        return None
    return info


def _driver():
    """惰性启动 playwright 驱动（进程级单例；CLI 进程退出随之消亡）。

    不调 .stop()：驱动进程随 CLI 退出即可，脱守护的浏览器不受影响
    （connect_over_cdp 断开不杀浏览器，这正是常驻模型的基础）。
    """
    global _PW
    if _PW is None:
        _PW = sync_playwright().start()
    return _PW


_PW = None


def _connect(port: int) -> Browser:
    """CDP 附着活守护；失败包装为人话 RuntimeError。"""
    try:
        return _driver().chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as exc:
        raise RuntimeError(
            f"附着常驻浏览器失败（port={port}）：{exc}；"
            "可执行 pageplay shutdown 后重试") from exc


def _terminate_pid(pid: int) -> None:
    """终止守护进程：SIGTERM 宽限 5s，仍活才 SIGKILL（Windows 走 taskkill）。"""
    if pid <= 0 or not _pid_alive(pid):
        return
    if os.name == "nt":  # Windows 无 SIGTERM 语义，taskkill 连子进程一起
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            log.warning("daemon: SIGKILL pid=%s 失败（忽略）", pid, exc_info=True)


def ensure_browser(headless: bool = False) -> Browser:
    """附着活守护，没有则起一个脱离的守护浏览器再附着（幂等）。

    顺序：daemon_info 验活附着 → 起守护（chromium 可执行文件直接
    Popen，start_new_session 脱离父进程，命令退出浏览器不死）→ 轮询
    /json/version 至多 30s → connect_over_cdp → 写 session.json。
    任何一步失败：清理残留并抛带人话指引的 RuntimeError（内核缺失时
    提示 playwright install chromium）。
    """
    info = daemon_info()
    if info is not None:
        log.info("daemon: 附着活守护（port=%s pid=%s headless=%s）",
                 info["port"], info["pid"], info["headless"])
        return _connect(info["port"])

    _session_file().unlink(missing_ok=True)  # daemon_info 已清，双保险
    executable = _driver().chromium.executable_path
    if not Path(executable).exists():
        raise RuntimeError(
            f"Chromium 内核缺失：{executable}"
            "（若为首次使用，请先执行 playwright install chromium）")
    profile = _home_dir() / _PROFILE_DIRNAME
    profile.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    cmd = [str(executable), f"--remote-debugging-port={port}",
           f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check",
           # 与 playwright 同一条加密路径：playwright 种下的 cookie 用
           # mock keychain 加密，裸起守护走真 Keychain 解不开会把行直接
           # 删掉（2026-09-24 实测）；带上同款开关档案才能跨命令/跨代复用
           "--use-mock-keychain"]
    if headless:
        cmd.append("--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    log.info("daemon: 起常驻浏览器（pid=%s port=%s headless=%s profile=%s）",
             proc.pid, port, headless, profile)
    deadline = time.monotonic() + _DAEMON_WAIT_SEC
    while time.monotonic() < deadline:
        if _cdp_alive(port):
            browser = _connect(port)
            _session_file().write_text(json.dumps({
                "port": port, "pid": proc.pid, "headless": headless,
            }) + "\n", encoding="utf-8")
            log.info("daemon: 守护就绪，session.json 已写入")
            return browser
        if proc.poll() is not None:
            _session_file().unlink(missing_ok=True)
            raise RuntimeError(
                f"Chromium 守护进程启动即退出（exit={proc.returncode}）；"
                "请重试，仍失败可删除浏览器档案目录后重试")
        time.sleep(0.25)
    _terminate_pid(proc.pid)
    _session_file().unlink(missing_ok=True)
    raise RuntimeError(
        f"Chromium 守护 {_DAEMON_WAIT_SEC:.0f} 秒内未就绪（调试端口无响应）；"
        "已清理残留，请重试")


def ensure_headful_browser() -> Browser:
    """login/pick/open 用：保证有头活窗（无头守护先换成有头）。

    无守护 → 起有头；有头守护 → 直接附着；无头守护（定时任务留下的）→
    shutdown 后起有头——人工交互类命令必须有可见窗口。
    """
    info = daemon_info()
    if info is not None and info["headless"]:
        log.info("daemon: 现存无头守护，先关闭再起有头")
        shutdown_browser()
    return ensure_browser(headless=False)


def shutdown_browser() -> bool:
    """关闭常驻守护（唯一由工具关浏览器的路径）。关了返回 True，没有返回 False。"""
    info = daemon_info()
    if info is None:
        return False
    _terminate_pid(info["pid"])
    _session_file().unlink(missing_ok=True)
    log.info("daemon: 已终止浏览器守护（pid=%s）", info["pid"])
    return True


def ensure_logged_in(browser: Browser, site: SitePreset,
                     max_wait: int = _COLLAB_MAX_WAIT_SEC) -> bool:
    """风控人机协作：开站点首页，等人在窗口里完成登录/验证（设计 §11）。

    每 2s 轮询登录标记，至多 max_wait 秒；通过返回 True（调用方重试
    原步骤），超时返回 False。无头守护时人无法介入：提示改有头重跑并
    直接失败。提示走终端 print（这条消息本身就是给人看的界面）。
    """
    info = daemon_info()
    if info is not None and info["headless"]:
        print("当前是无头守护浏览器，无法人工协助登录/验证；"
              "请去掉 --headless 改有头模式重跑")
        return False
    context = browser.contexts[0]
    page = context.new_page()
    try:
        page.goto(site.home_url)
        print(f"请在浏览器窗口完成登录/验证，通过后自动继续"
              f"（至多等 {max_wait} 秒）…")
        deadline = time.monotonic() + max_wait
        while True:
            if cookies.has_login_state(context.cookies(), site.domain,
                                       site.check_cookies):
                log.info("collab %s: 人已通过登录/验证", site.name)
                return True
            if time.monotonic() >= deadline:
                print("等待人工登录/验证超时，本次未能自动继续")
                return False
            time.sleep(_POLL_INTERVAL_SEC)
    finally:
        try:
            page.close()
        except Exception:
            log.warning("collab %s: 关页失败（忽略）", site.name, exc_info=True)


class SiteSession:
    """一个站点在共享常驻浏览器里的会话（快照/meta/recipes 归站点目录）。

    browser_factory 可注入（默认 ensure_browser），测试打桩用——硬契约，
    不得删。所有方法只在共享浏览器里开页干活、完事关页；浏览器本体
    归守护管理，本类任何路径都不关它。
    """

    def __init__(self, site: SitePreset, sites_root: Path,
                 browser_factory: Callable | None = None) -> None:
        self.site = site
        self._sites_root = Path(sites_root)
        self._site_dir = self._sites_root / site.name
        self._browser_factory: Callable = (
            browser_factory if browser_factory is not None else ensure_browser)
        self._pages: list = []  # 本会话开过的页，close() 统一关（关页不关浏览器）

    # ------------------------------------------------------------------
    # 共享浏览器上下文与页面管理
    # ------------------------------------------------------------------

    def _shared_context(self, headless: bool) -> BrowserContext:
        """附着（或起）常驻浏览器，返回 CDP 默认上下文（cookie 全站共享）。"""
        browser = self._browser_factory(headless)
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError(
                "常驻浏览器没有可用上下文；请执行 pageplay shutdown 后重试")
        return contexts[0]

    def _new_tracked_page(self, context: BrowserContext):
        """开一个新页并记账（close() 时统一关，关页不关浏览器）。"""
        page = context.new_page()
        self._pages.append(page)
        return page

    def close(self) -> None:
        """关闭本会话开过的所有页面（幂等；绝不关浏览器本体）。"""
        pages, self._pages = self._pages, []
        for page in pages:
            try:
                page.close()
            except Exception:
                log.warning("session %s: 关闭页面失败（忽略）",
                            self.site.name, exc_info=True)

    # ------------------------------------------------------------------
    # 业务动作
    # ------------------------------------------------------------------

    def login(self, timeout_sec: int = 300, url: str | None = None) -> bool:
        """有头窗口打开登录页，每 2s 轮询 cookies 等人完成登录。

        url 给定时打开该页面（login 贴网址命中预设：打开客户贴的页面），
        否则打开预设 login_url，原行为不变。
        登录成功：刷新快照、写 meta.json、返回 True；超时返回 False。
        结束（无论成败）只关本会话开的页，浏览器保持常驻。
        """
        deadline = time.monotonic() + timeout_sec
        context = self._shared_context(headless=False)
        page = self._new_tracked_page(context)
        try:
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
        """两级验活：冷检查上下文 cookie，冷败再暖检查等站点自动续登。

        冷检查：附着常驻浏览器（无活守护则起无头守护）读上下文 cookie
        直接判 has_login_state——命中即有效并刷新快照。
        暖检查：冷检查查不到不等于真失效——_tb_token_ 等会话级 cookie
        浏览器关闭即不落盘；但档案里的长期 cookie 能让站点打开页面时
        静默自动续登。此时开 home_url 页，每 1s 轮询登录标记至多 15s，
        等到即判有效并刷新快照（把续登出的会话 cookie 收进快照）。
        两级都败才判失效（log 分别记两级结果）；goto 超时等异常只判
        暖级失败，不炸整体验证。结束只关页，浏览器保持常驻。
        """
        context = self._shared_context(headless=True)
        cold_ok = cookies.has_login_state(context.cookies(),
                                          self.site.domain,
                                          self.site.check_cookies)
        if cold_ok:
            self.refresh_snapshot()
            log.info("verify %s: 冷检查命中登录态，快照已刷新", self.site.name)
            return True
        warm_ok = self._warm_relogin_check(context)
        log.info("verify %s: 登录态%s（冷检查=未命中，暖检查=%s）",
                 self.site.name, "有效" if warm_ok else "失效",
                 "命中" if warm_ok else "未命中")
        return warm_ok

    def _warm_relogin_check(self, context: BrowserContext) -> bool:
        """verify 暖级：开落地页等自动续登，拿到标记即刷快照返回 True。

        goto 超时、页面已关等 playwright 异常只判本级失败（返回
        False），不向上炸整体验证。
        """
        page = self._new_tracked_page(context)
        try:
            page.goto(self.site.home_url)
            for _ in range(_WARM_POLL_MAX_TRIES):
                if cookies.has_login_state(context.cookies(),
                                           self.site.domain,
                                           self.site.check_cookies):
                    self.refresh_snapshot()
                    log.info("verify %s: 自动续登成功，快照已刷新",
                             self.site.name)
                    return True
                time.sleep(_WARM_POLL_INTERVAL_SEC)
            return False
        except Exception as exc:
            log.warning("verify %s: 暖检查异常，按本级失败处理：%s",
                        self.site.name, exc)
            return False
        finally:
            try:
                page.close()
            except Exception:
                pass

    def refresh_snapshot(self) -> None:
        """从共享浏览器上下文读本域 cookie 并刷新快照文件。

        cookie 在上下文层直接可读（无需开页）；只存本站域的 cookie
        （共享档案装所有站，快照按域切分，export 语义不变）。无活守护
        时会起一个无头守护（保持常驻，供后续命令复用）。
        """
        context = self._shared_context(headless=True)
        picked = cookies.filter_by_domain(context.cookies(), self.site.domain)
        cookies.save_snapshot(self._site_dir, picked)
        log.info("session %s: 快照已刷新（共享浏览器，%d 条本域 cookie）",
                 self.site.name, len(picked))

    def export_cookies(self) -> list[dict]:
        """只读快照导出 cookie 列表，不启动浏览器。"""
        return cookies.load_snapshot(self._site_dir)

    def open(self, url: str | None = None,
             headless: bool = False) -> BrowserContext:
        """附着常驻浏览器并返回共享上下文，供外部（AI）驱动；默认有头。

        不给 url 时不打开页面；给 url 时开一个新页并导航（login 贴网址
        入口用）。页面已记账，用完调 close() 关页——浏览器保持常驻。
        """
        context = self._shared_context(headless=headless)
        if url is not None:
            page = self._new_tracked_page(context)
            page.goto(url)
            log.info("session %s: 已打开 %s", self.site.name, url)
        return context

    def forget(self) -> None:
        """删除站点目录（快照 + meta + recipes），工具侧忘记该站登录态。

        注意：登录态真身在共享档案 <home>/browser-profile（所有站共用，
        归守护管理），本命令不动它——需要连浏览器档案一起清时，关窗或
        pageplay shutdown 后清浏览器数据。旧 sites/<site>/browser-profile
        残留随目录一并删除（本就不再读写）。
        """
        self.close()
        shutil.rmtree(self._site_dir, ignore_errors=False)
        log.info("session %s: 已删除站点目录 %s", self.site.name, self._site_dir)

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------

    def write_meta(self) -> None:
        """写 meta.json（设计 §3 schema：site/login_url/saved_at/check_cookies）。

        login 成功路径与 CLI 交互式登录共用；调用前站点目录必须已存在
        （正常流程 refresh_snapshot 落快照时已建）。
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
