"""命令行入口：注册六个子命令并派发到各命令处理函数。"""

from __future__ import annotations

import argparse


def _cmd_login(args: argparse.Namespace) -> int:
    """login：打开浏览器人工登录，轮询登录态并落会话快照。"""
    raise NotImplementedError


def _cmd_list(args: argparse.Namespace) -> int:
    """list：列出内置站点预设。"""
    raise NotImplementedError


def _cmd_doctor(args: argparse.Namespace) -> int:
    """doctor：自检环境与已存会话健康度。"""
    raise NotImplementedError


def _cmd_export(args: argparse.Namespace) -> int:
    """export：导出站点 cookie 为精简 JSON。"""
    raise NotImplementedError


def _cmd_open(args: argparse.Namespace) -> int:
    """open：以持久档案打开 headful 浏览器供驱动。"""
    raise NotImplementedError


def _cmd_forget(args: argparse.Namespace) -> int:
    """forget：删除站点会话目录，忘记登录态。"""
    raise NotImplementedError


def _build_parser() -> argparse.ArgumentParser:
    """构建带六个子命令的 argparse 解析器。"""
    parser = argparse.ArgumentParser(
        prog="pageplay",
        description="通用网站会话工具：人登录一次，AI 拿 cookie 或驱动浏览器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="人工登录并保存会话快照")
    p_login.add_argument("--url", default=None, help="自定义登录页 URL（不走内置预设）")
    p_login.add_argument("--timeout", type=int, default=300, help="等待人工登录的超时秒数")
    p_login.set_defaults(func=_cmd_login)

    p_list = sub.add_parser("list", help="列出内置站点预设")
    p_list.set_defaults(func=_cmd_list)

    p_doctor = sub.add_parser("doctor", help="自检环境与会话状态")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_export = sub.add_parser("export", help="导出 cookie 为精简 JSON")
    p_export.add_argument("--out", default=None, help="输出文件路径（缺省打印到终端）")
    p_export.set_defaults(func=_cmd_export)

    p_open = sub.add_parser("open", help="打开 headful 持久浏览器会话")
    p_open.set_defaults(func=_cmd_open)

    p_forget = sub.add_parser("forget", help="删除站点会话目录")
    p_forget.set_defaults(func=_cmd_forget)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并派发子命令；未实现命令打印提示并返回 2。"""
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedError:
        print("该命令尚未实现")
        return 2
