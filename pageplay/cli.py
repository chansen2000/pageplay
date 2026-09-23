"""命令行入口：解析六个子命令并派发到站点/会话/快照模块。

站点解析：login 用 --url（可自定义站）；其余命令先查内置表、未命中
回退已存 meta.json 重建预设（自定义站点登录后才能被 doctor/export 等
继续操作）。顶层异常统一映射退出码：0 成功 / 1 业务失败 / 2 风控 /
130 用户中断。输出一律业务语言，不 dump 原始 dict。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cookies import export_json
from .guard import RiskTriggered
from .logging_setup import setup_logging
from .session import SiteSession
from .sites import SitePreset, get_site, list_builtin, resolve_site

_STATE_FILENAME = "state.json"
_META_FILENAME = "meta.json"


# ----------------------------------------------------------------------
# 路径与站点解析
# ----------------------------------------------------------------------

def _sites_root() -> Path:
    """登录态存储根：PAGEPLAY_HOME 覆盖，默认 ~/.pageplay，下挂 sites/。"""
    return Path(os.environ.get("PAGEPLAY_HOME", "~/.pageplay")).expanduser() / "sites"


def _site_dir(name: str) -> Path:
    return _sites_root() / name


def _resolve_saved(name: str) -> SitePreset:
    """非 login 命令的站点解析：先内置表，未命中回退已存 meta.json。

    自定义站点（login --url 保存的）不在内置表里，但 meta.json 记录了
    login_url 与 check_cookies，可据此重建预设。两处都查不到时抛
    KeyError（消息含可用站点与 --url 用法提示）。
    """
    try:
        return get_site(name)
    except KeyError:
        meta_path = _site_dir(name) / _META_FILENAME
        if not meta_path.is_file():
            raise  # 原样抛出 get_site 的 KeyError（含可用站点提示）
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            login_url = str(meta["login_url"])
        except (OSError, ValueError, KeyError, TypeError):
            raise KeyError(
                f"站点 {name!r} 的 meta.json 损坏或缺少 login_url；"
                f"请删除该站点目录后重新 pageplay login {name} --url <登录页URL>"
            ) from None
        hostname = urlsplit(login_url).hostname or ""
        if "." not in hostname:
            raise KeyError(
                f"站点 {name!r} 的 login_url 非法：{login_url!r}；"
                f"请重新 pageplay login {name} --url https://example.com/login"
            ) from None
        return SitePreset(
            name=name,
            login_url=login_url,
            home_url=login_url,
            domain=".".join(hostname.split(".")[-2:]),  # 注册域近似，与 resolve_site 一致
            check_cookies=tuple(meta.get("check_cookies") or ()),
        )


def _resolve_site_or_report(name: str, login_url: str | None = None) -> SitePreset | None:
    """解析站点预设；非法站点名/URL 打印人话到 stderr 并返回 None。

    调用方拿到 None 直接 return 1。login 传入 --url 时走 resolve_site
    自定义预设；其余命令 login_url 为 None，走 _resolve_saved（内置表
    + meta.json 回退）。
    """
    try:
        if login_url is not None:
            return resolve_site(name, login_url)
        return _resolve_saved(name)
    except (KeyError, ValueError) as exc:
        print(f"站点解析失败：{exc}", file=sys.stderr)
        return None


# ----------------------------------------------------------------------
# 六个子命令
# ----------------------------------------------------------------------

def _cmd_login(args: argparse.Namespace) -> int:
    """login：打开浏览器人工登录，轮询登录态并落会话快照。"""
    site = _resolve_site_or_report(args.site, args.url)
    if site is None:
        return 1
    print(f"正在打开 {site.name} 登录页，请在浏览器窗口里完成登录"
          f"（滑块/扫码自行通过），最长等待 {args.timeout} 秒…")
    session = SiteSession(site, _sites_root())
    if not session.login(timeout_sec=args.timeout):
        hint = f"pageplay login {site.name}"
        if args.url:
            hint += f" --url {args.url}"
        print(f"{site.name}：超时未检测到登录态，浏览器已关闭。"
              f"请重跑 {hint} 再试一次。", file=sys.stderr)
        return 1
    print(f"{site.name}：登录成功，登录态已保存到 {_site_dir(site.name)}")
    print(f"后续可用：pageplay doctor {site.name} ｜ "
          f"pageplay export {site.name} ｜ pageplay open {site.name}")
    return 0


def _snapshot_time(site_dir: Path) -> str:
    """读快照保存时间用于展示；无快照返回"未登录"。"""
    try:
        data = json.loads((site_dir / _STATE_FILENAME).read_text(encoding="utf-8"))
        return str(data.get("saved_at", "未知时间"))
    except (OSError, ValueError):
        return "未登录"


def _cmd_list(args: argparse.Namespace) -> int:
    """list：已保存站点（有 meta.json 的目录）+ 内置预设，一张清单。"""
    rows: dict[str, str] = {}
    root = _sites_root()
    if root.is_dir():
        for meta_path in sorted(root.glob(f"*/{_META_FILENAME}")):
            rows[meta_path.parent.name] = _snapshot_time(meta_path.parent)
    for preset in list_builtin():  # 内置站点未保存也列出，标"未登录"
        rows.setdefault(preset.name, "未登录")
    if not rows:
        print("暂无站点。用 pageplay login <站点名> 登录第一个站点。")
        return 0
    width = max(len(name) for name in rows)
    print("已登记站点：")
    for name, when in sorted(rows.items()):
        print(f"  {name:<{width}}  {when}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """doctor：headless 验活；有效则刷新快照（设计 §3），过期提示重登。"""
    site = _resolve_site_or_report(args.site)
    if site is None:
        return 1
    session = SiteSession(site, _sites_root())
    if not session.verify():
        print(f"{site.name}：登录态已过期，请重新执行 pageplay login {site.name}",
              file=sys.stderr)
        return 1
    session.refresh_snapshot()
    print(f"{site.name}：登录态有效，cookie 快照已刷新")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """export：只读快照导出精简 JSON；缺省/--out - 打印，--out FILE 落盘 0600。"""
    site = _resolve_site_or_report(args.site)
    if site is None:
        return 1
    session = SiteSession(site, _sites_root())
    try:
        cookie_list = session.export_cookies()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)  # 消息自带"先 pageplay login"指引
        return 1
    payload = export_json(cookie_list)
    if args.out is None or args.out == "-":
        print(payload)
        return 0
    out_path = Path(args.out).expanduser()
    out_path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass  # 无 POSIX 权限位的环境（如 Windows）静默跳过
    print(f"{site.name}：已导出 {len(cookie_list)} 条 cookie 到 {out_path}（权限 0600）")
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    """open：以持久档案打开 headful 浏览器，阻塞到 Ctrl-C，结束必清理。"""
    site = _resolve_site_or_report(args.site)
    if site is None:
        return 1
    session = SiteSession(site, _sites_root())
    session.open()
    try:
        print(f"{site.name}：浏览器已打开（带登录态档案），可交给 AI 驱动；"
              f"操作完成后按 Ctrl-C 结束")
        while True:
            time.sleep(60)  # 阻塞驻留；KeyboardInterrupt 即退出信号
    except KeyboardInterrupt:
        print(f"{site.name}：浏览器已关闭")
        return 0
    finally:
        session.close()


def _cmd_forget(args: argparse.Namespace) -> int:
    """forget：删除站点会话目录（档案+快照+meta），彻底忘记登录态。"""
    site = _resolve_site_or_report(args.site)
    if site is None:
        return 1
    session = SiteSession(site, _sites_root())
    try:
        session.forget()
    except FileNotFoundError:
        print(f"{site.name}：没有已保存的会话档案，无需删除", file=sys.stderr)
        return 1
    print(f"{site.name}：会话档案已删除（浏览器档案 + 快照 + meta）")
    return 0


# ----------------------------------------------------------------------
# 参数解析与入口
# ----------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """构建带六个子命令的 argparse 解析器。"""
    parser = argparse.ArgumentParser(
        prog="pageplay",
        description="通用网站会话工具：人登录一次，AI 拿 cookie 或驱动浏览器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="人工登录并保存会话快照")
    p_login.add_argument("site", help="站点名（保存/查找时使用，如 taobao）")
    p_login.add_argument("--url", default=None, help="自定义登录页 URL（不走内置预设）")
    p_login.add_argument("--timeout", type=int, default=300, help="等待人工登录的超时秒数")
    p_login.set_defaults(func=_cmd_login)

    p_list = sub.add_parser("list", help="列出已保存站点与内置预设")
    p_list.set_defaults(func=_cmd_list)

    p_doctor = sub.add_parser("doctor", help="检查登录态是否仍有效")
    p_doctor.add_argument("site", help="站点名")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_export = sub.add_parser("export", help="导出 cookie 为精简 JSON")
    p_export.add_argument("site", help="站点名")
    p_export.add_argument("--out", default=None,
                          help="输出文件路径（缺省或 - 打印到终端；文件权限 0600）")
    p_export.set_defaults(func=_cmd_export)

    p_open = sub.add_parser("open", help="打开 headful 持久浏览器会话")
    p_open.add_argument("site", help="站点名")
    p_open.set_defaults(func=_cmd_open)

    p_forget = sub.add_parser("forget", help="删除站点会话目录")
    p_forget.add_argument("site", help="站点名")
    p_forget.set_defaults(func=_cmd_forget)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：启动即配日志，解析派发，顶层统一映射退出码。"""
    setup_logging()
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RiskTriggered as exc:
        print(f"检测到风控信号，已停止操作：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # RuntimeError/启动失败/锁冲突等一律业务失败
        print(f"执行失败：{exc}", file=sys.stderr)
        return 1
