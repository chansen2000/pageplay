"""命令行入口：解析十四个子命令并派发到站点/会话/快照/recipe/流程模块。

站点解析：所有命令同用 parse_target——login 支持直接贴网址/域名，
贴 URL 命中内置预设走自动轮询、陌生站走"人工按回车"交互；预设名与
--url 用法保持兼容。其余命令（doctor/export/open/forget/recipes/flows）
贴 URL 时只推导站点名（与 login 落盘一致），按"已存 meta.json 优先
→ 内置预设"解析；pick/record 同样解析但会打开贴的 URL（没贴则开
home_url）。pick/run/record/results 的处理函数在 cli_pick 模块（本文件
只留注册与既有命令），_build_parser 内延迟 import——cli_pick 反向经
模块属性取本文件的共享底层，顶层互不 import 才没有循环依赖。
常驻模型（设计 §11）：浏览器是脱离的守护进程，命令经 session 模块
附着干活、完事关页；人关窗（有头）或 shutdown 命令（无头守护）是
唯一退出。
顶层异常统一映射退出码：0 成功 / 1 业务失败 / 2 风控 / 130 用户中断。
输出一律业务语言，不 dump 原始 dict。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import flows, recipes
from .cookies import export_json, filter_by_domain
from .guard import RiskTriggered
from .logging_setup import setup_logging
from .session import SiteSession, ensure_headful_browser, shutdown_browser
from .sites import SitePreset, get_site, list_builtin, parse_target, resolve_site

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


def _saved_site_names() -> list[str]:
    """已保存站点名清单：sites 根下有 meta.json 的目录名，排序返回。"""
    root = _sites_root()
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob(f"*/{_META_FILENAME}"))


def _load_saved_preset(name: str) -> SitePreset | None:
    """从已存 meta.json 重建站点预设；没有登录记录返回 None。

    自定义站点（login 贴陌生网址保存的）不在内置表里，但 meta.json
    记录了 login_url 与 check_cookies，可据此重建预设。meta 损坏抛
    KeyError（消息给修复指引）。这是 meta 重建的唯一实现，login 与
    非 login 命令不各自维护一套。
    """
    meta_path = _site_dir(name) / _META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        login_url = str(meta["login_url"])
    except (OSError, ValueError, KeyError, TypeError):
        raise KeyError(
            f"站点 {name!r} 的 meta.json 损坏或缺少 login_url；请删除该站点目录后重新 pageplay login {name} --url <登录页URL>"
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


def _resolve_target(arg: str) -> SitePreset:
    """非 login 命令的统一站点解析入口（doctor/export/open/forget 共用）。

    与 login 同用 parse_target：贴网址/域名先推导站点名（www.taobao.com
    → taobao，与 login 落盘的推导名一致）；贴的 URL 在这里只用于推导
    名字，不会打开。解析顺序：已存优先（meta.json → _load_saved_preset）
    → 内置预设（get_site）→ 都没有抛 KeyError，消息给可执行的 login
    指引与可用站点清单（不再误导用户用 --url）。
    """
    name, _pasted_url = parse_target(arg)
    saved = _load_saved_preset(name)
    if saved is not None:
        return saved
    try:
        return get_site(name)
    except KeyError:
        saved_names = _saved_site_names()
        builtin_names = ", ".join(sorted(s.name for s in list_builtin()))
        raise KeyError(
            f"站点 {name!r} 还没有登录记录；先运行 pageplay login {arg.strip()}。"
            f"已存站点：{', '.join(saved_names) if saved_names else '无'}；"
            f"内置：{builtin_names}"
        ) from None


def _resolve_site_or_report(arg: str) -> SitePreset | None:
    """解析站点预设；非法目标打印人话到 stderr 并返回 None。

    调用方拿到 None 直接 return 1。login 不走这里（它有自己的两档
    解析 + 贴 URL 打开行为），但底层推导同用 sites.parse_target。
    """
    try:
        return _resolve_target(arg)
    except (KeyError, ValueError) as exc:
        print(f"站点解析失败：{exc}", file=sys.stderr)
        return None


# ----------------------------------------------------------------------
# 六个子命令
# ----------------------------------------------------------------------

def _login_by_enter(session: SiteSession, url: str) -> None:
    """陌生站交互式登录：打开贴的页面，人工按回车后收该域 cookie。

    有该域 cookie 即刷新快照、写 meta 返回；空则提示后再等一次回车。
    结束（含中途异常/中断）必关浏览器释放档案锁。
    """
    print(f"正在打开 {url}…")
    print("请在浏览器窗口里完成登录（滑块/扫码自行通过），"
          "完成后回到本窗口按回车保存会话")
    try:
        context = session.open(url)
        while True:
            input()
            if filter_by_domain(context.cookies(), session.site.domain):
                break
            print("未抓到该域 cookie，登录完成后再按一次回车")
        session.refresh_snapshot()
        session.write_meta()
    finally:
        session.close()


def _cmd_login(args: argparse.Namespace) -> int:
    """login：贴网址/域名或给站点名，打开浏览器人工登录并落会话快照。

    两档：贴的目标命中内置预设（注册域对上）→ 自动档，沿用该预设
    check_cookies 轮询、打开贴的页面；陌生站 → 回车档，人工按回车后
    收该域 cookie（有 cookie 即保存）。预设名与 --url 用法不变。
    自动档先做 doctor 语义验活：登录态仍有效就不折腾人，直接提示。
    """
    try:
        name, pasted_url = parse_target(args.site)
    except ValueError as exc:
        print(f"站点解析失败：{exc}", file=sys.stderr)
        return 1
    effective_url = pasted_url or args.url
    hit_builtin = pasted_url is not None and name in {
        s.name for s in list_builtin()}

    try:
        site = (get_site(name) if hit_builtin
                else resolve_site(name, effective_url))
    except (KeyError, ValueError) as exc:
        print(f"站点解析失败：{exc}", file=sys.stderr)
        return 1

    session = SiteSession(site, _sites_root())
    if pasted_url is not None and not hit_builtin:
        _login_by_enter(session, effective_url)  # 回车档：陌生站
    else:
        # 自动档：预设名 / --url 自定义 / 贴 URL 命中预设（打开贴的页面）
        ensure_headful_browser()  # 登录必须有人：先保证有头活窗（无头守护换成有头）
        if session.verify():
            print(f"{site.name}：登录态仍有效，无需重复登录")
            return 0
        print(f"正在打开 {site.name} 登录页，请在浏览器窗口里完成登录"
              f"（滑块/扫码自行通过），最长等待 {args.timeout} 秒…")
        if not session.login(timeout_sec=args.timeout,
                             url=effective_url if hit_builtin else None):
            hint = f"pageplay login {site.name}" + (
                f" --url {args.url}" if args.url else "")
            print(f"{site.name}：超时未检测到登录态。浏览器窗口保持打开，"
                  f"可重跑 {hint} 再试一次。", file=sys.stderr)
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
    if args.json:  # 机读出口：JSON 数组（GUI 下拉数据源），空库也是 []
        print(json.dumps([{"name": n, "saved_at": t}
                          for n, t in sorted(rows.items())],
                         ensure_ascii=False))
        return 0
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
    """open：附着（或起）有头常驻浏览器并打开站点首页，命令即返不阻塞。

    常驻模型（设计 §11）：窗口人开人关——本命令只负责"开"，窗口保持
    打开，后续命令自动附到同一窗口；关窗或 pageplay shutdown 即结束。
    """
    site = _resolve_site_or_report(args.site)
    if site is None:
        return 1
    session = SiteSession(site, _sites_root())
    session.open(site.home_url)
    print(f"{site.name}：浏览器窗口已打开并停在首页，窗口会一直保持")
    print("后续命令（doctor/pick/run…）会自动附到同一窗口；")
    print("关闭：直接关掉窗口，或执行 pageplay shutdown")
    return 0


def _cmd_shutdown(args: argparse.Namespace) -> int:
    """shutdown：关闭常驻浏览器守护（无头定时场景收尾用；人关窗等价）。"""
    if shutdown_browser():
        print("常驻浏览器已关闭（session.json 已清理）")
    else:
        print("当前没有常驻浏览器")
    return 0


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
# 清单命令（pick/run/record/results 处理函数在 cli_pick 模块）
# ----------------------------------------------------------------------

def _recipe_site_dirs() -> list[Path]:
    """sites 根下有 recipes/ 目录的站点目录（排序稳定）。"""
    root = _sites_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "recipes").is_dir())


def _flow_site_dirs() -> list[Path]:
    """sites 根下有 flows/ 目录的站点目录（排序稳定；record 落盘处）。"""
    root = _sites_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "flows").is_dir())


def _cmd_recipes(args: argparse.Namespace) -> int:
    """recipes：列 recipe（名字/动作/URL/创建时间）；无参全部站，带参单站。"""
    if args.site is not None:
        site = _resolve_site_or_report(args.site)
        if site is None:
            return 1
        site_dirs = [_site_dir(site.name)]
    else:
        site_dirs = _recipe_site_dirs()
    rows = [(d, r) for d in site_dirs for r in recipes.list_recipes(d)]
    if args.json:  # 机读出口：list_recipes 原始 dict 数组，空库也是 []
        print(json.dumps([r for _d, r in rows], ensure_ascii=False))
        return 0
    if not rows:
        print("暂无 recipe。用 pageplay pick <站点或网址> 框选第一个诉求。")
        return 0
    width = max(len(str(r["name"])) for _d, r in rows)
    print("已保存 recipe：")
    for _d, r in rows:
        print(f"  {str(r['name']):<{width}}  {r['action']}  {r['url']}"
              f"  创建于 {r['created_at']}")
    return 0


def _cmd_flows(args: argparse.Namespace) -> int:
    """flows：列流程（名字/步数/起始URL/创建时间）；无参全部站，带参单站。"""
    if args.site is not None:
        site = _resolve_site_or_report(args.site)
        if site is None:
            return 1
        site_dirs = [_site_dir(site.name)]
    else:
        site_dirs = _flow_site_dirs()
    rows = [(d, f) for d in site_dirs for f in flows.list_flows(d)]
    if args.json:  # 机读出口：list_flows 原始 dict 数组，空库也是 []
        print(json.dumps([f for _d, f in rows], ensure_ascii=False))
        return 0
    if not rows:
        print("暂无流程。用 pageplay record <站点或网址> 录制第一个流程。")
        return 0
    width = max(len(str(f["name"])) for _d, f in rows)
    print("已保存流程：")
    for _d, f in rows:
        print(f"  {str(f['name']):<{width}}  {f['step_count']} 步  {f['url']}"
              f"  创建于 {f['created_at']}")
    return 0


# ----------------------------------------------------------------------
# 参数解析与入口
# ----------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """构建带十四个子命令的 argparse 解析器。

    pick/run/record/results 在 cli_pick、grab 在 cli_grab，这里函数内
    延迟 import：它们反向要取本模块的共享底层（含测试替身 monkeypatch
    的 SiteSession），顶层互不 import 才能保证任何导入顺序都不循环。
    """
    from .cli_pick import _cmd_pick, _cmd_record, _cmd_results, _cmd_run
    from .cli_grab import register_grab

    parser = argparse.ArgumentParser(
        prog="pageplay",
        description="通用网站会话工具：人登录一次，AI 拿 cookie 或驱动浏览器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="人工登录并保存会话快照")
    p_login.add_argument(
        "site",
        help="站点名（如 taobao）或网址/域名（如 www.taobao.com，命中预设自动识别，陌生站登录后按回车保存）")
    p_login.add_argument("--url", default=None, help="自定义登录页 URL（不走内置预设）")
    p_login.add_argument("--timeout", type=int, default=300, help="等待人工登录的超时秒数")
    p_login.set_defaults(func=_cmd_login)

    p_list = sub.add_parser("list", help="列出已保存站点与内置预设")
    p_list.add_argument("--json", action="store_true", help="stdout 打机读 JSON 数组（GUI 数据源）")
    p_list.set_defaults(func=_cmd_list)

    p_doctor = sub.add_parser("doctor", help="检查登录态是否仍有效")
    p_doctor.add_argument("site", help="站点名或网址/域名（如 www.taobao.com）")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_export = sub.add_parser("export", help="导出 cookie 为精简 JSON")
    p_export.add_argument("site", help="站点名或网址/域名（如 www.taobao.com）")
    p_export.add_argument("--out", default=None,
                          help="输出文件路径（缺省或 - 打印到终端；文件权限 0600）")
    p_export.set_defaults(func=_cmd_export)

    p_open = sub.add_parser("open", help="打开 headful 持久浏览器会话")
    p_open.add_argument("site", help="站点名或网址/域名（如 www.taobao.com）")
    p_open.set_defaults(func=_cmd_open)

    p_forget = sub.add_parser("forget", help="删除站点会话目录")
    p_forget.add_argument("site", help="站点名或网址/域名（如 www.taobao.com）")
    p_forget.set_defaults(func=_cmd_forget)

    p_pick = sub.add_parser("pick", help="浏览器里框选表格/元素，存为可重放 recipe")
    p_pick.add_argument("site", help="站点名或网址/域名（如 www.taobao.com）")
    p_pick.add_argument("--name", default=None, help="recipe 名（默认 <站点>-<序号>）")
    p_pick.set_defaults(func=_cmd_pick)

    p_record = sub.add_parser("record", help="录制一段浏览操作，存为可整链重放的流程")
    p_record.add_argument("site", help="站点名或网址/域名（如 www.taobao.com）")
    p_record.add_argument("--name", default=None,
                          help="流程名（默认交互确认，回车即 <站点>-flow-N）")
    p_record.set_defaults(func=_cmd_record)

    p_run = sub.add_parser("run",
                           help="按名字重放：先查流程（整链），再查 recipe（单条）")
    p_run.add_argument("name", help="流程名（pageplay flows 可查）或 recipe 名（pageplay recipes 可查）")
    p_run.add_argument("--headless", action="store_true",
                       help="用无头守护跑（定时任务场景；无头时无法人工协助风控）")
    p_run.add_argument("--out", default=None,
                       help="产物目录（默认 ~/Downloads/pageplay/<名字>）")
    p_run.set_defaults(func=_cmd_run)

    p_recipes = sub.add_parser("recipes", help="列出已保存的 recipe")
    p_recipes.add_argument("site", nargs="?", default=None,
                           help="只列该站点（缺省列全部站点）")
    p_recipes.add_argument("--json", action="store_true",
                           help="stdout 打机读 JSON 数组（GUI 数据源）")
    p_recipes.set_defaults(func=_cmd_recipes)

    p_flows = sub.add_parser("flows", help="列出已录制的流程")
    p_flows.add_argument("site", nargs="?", default=None,
                         help="只列该站点（缺省列全部站点）")
    p_flows.add_argument("--json", action="store_true",
                         help="stdout 打机读 JSON 数组（GUI 数据源）")
    p_flows.set_defaults(func=_cmd_flows)

    p_results = sub.add_parser("results", help="回看历史执行记录（✓✗ 与产物路径）")
    p_results.add_argument("recipe", nargs="?", default=None,
                           help="只看该 recipe 的记录（缺省看全部）")
    p_results.add_argument("--limit", type=int, default=20,
                           help="最多显示条数（默认 20）")
    p_results.add_argument("--json", action="store_true", help="stdout 打机读 JSON 数组（GUI 数据源）")
    p_results.set_defaults(func=_cmd_results)

    p_shutdown = sub.add_parser("shutdown", help="关闭常驻浏览器守护")
    p_shutdown.set_defaults(func=_cmd_shutdown)
    register_grab(sub)  # grab 的注册与实现同在 cli_grab 模块

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
