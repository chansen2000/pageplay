"""grab 命令：从浏览器「当前页」框选取数（T14）。

与 pick 的区别：不导航、不开新页——ensure_browser(headless=False) 附着
后由 target_page.pick_target_page 选「人正看着的可见标签页」（T19：不
再取 pages[-1]，真机 8 标签页实测最后一个可能是空白新标签页），就在
该页跑 picker.run_pick 单条模式（repeat=False）。确认后当场执行并落账，
但不存 recipe：grab 是一次性取数，沉淀 recipe/流程仍归 pick/record。
账本 action 记 "grab"（ledger_action，执行语义随框选结果本身）。

站点参数可选，仅用于命名与落账归属：不校验登录记录、不导航、不校验
当前页归属。产物目录 ~/Downloads/pageplay/grab/<时间戳>-<站或域名>/。

退出码：确认执行成功 0；没有任何标签页 / 取消 / 执行失败 1；真风控
（RiskTriggered 从 _execute_and_record 穿透）→ main 映射 2。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from urllib.parse import urlsplit

from . import picker
from .cli_pick import _execute_and_record, _products_root
from .session import ensure_browser
from .sites import parse_target
from .target_page import pick_target_page


def _label_from_site_arg(arg: str) -> str:
    """站点参数 → 命名标签：与 parse_target 同一推导（www.taobao.com →
    taobao）；解析不出就退回原文（站点仅用于命名，不为难人）。"""
    try:
        name, _url = parse_target(arg)
        return name
    except ValueError:
        return arg.strip() or "page"


def _label_from_page_url(url: str) -> str:
    """当前页 URL → 命名标签：主机名取倒数第二段（demo.example.com →
    example，与 parse_target 同近似）；取不出主机名用 "page"。"""
    hostname = urlsplit(url).hostname or ""
    segments = [s for s in hostname.split(".") if s]
    if len(segments) >= 2:
        return segments[-2]
    return segments[0] if segments else "page"


def register_grab(sub) -> None:
    """向 CLI 根解析器注册 grab 子命令（cli._build_parser 一行调用）。"""
    p = sub.add_parser("grab", help="从浏览器当前页框选取数据（不导航、不存 recipe）")
    p.add_argument("site", nargs="?", default=None,
                   help="站点名或网址（可选，仅用于命名与落账归属）")
    p.set_defaults(func=_cmd_grab)


def _cmd_grab(args: argparse.Namespace) -> int:
    """grab：附着活窗取当前页，框选一次、当场执行落账（不存 recipe）。"""
    browser = ensure_browser(headless=False)  # 只附着，绝不关浏览器
    context = browser.contexts[0]
    try:
        page = pick_target_page(context)  # T19：人正看着的可见标签页
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)  # 人话（含"先打开窗口"指引）
        return 1
    try:
        title = page.title() or ""
    except Exception:
        title = ""  # 个别内建页 title() 会失败，不让它挡取数
    site_arg = str(args.site or "").strip()  # 空白等价于没给
    label = (_label_from_site_arg(site_arg) if site_arg
             else _label_from_page_url(page.url))
    print("── 框选步骤 ──\n1. 在当前页把鼠标移到目标上\n2. 红框罩住后单击锁定\n"
          "3. 勾列/勾字段 → 确认 → 数据落地\n─────────────")
    # T16 框选注入位置明示：title 不对 = 人关错页重试，不再盲猜
    print(f"框选已激活在标签页：{title or '（无标题）'}（{page.url[:60]}）")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _products_root() / "grab" / f"{stamp}-{label}"
    name = f"{label}-grab"
    picked: dict = {"ok": False}

    def _handler(result: dict) -> None:
        # 执行 + 落账，不存 recipe；账本 action 记 grab。RiskTriggered
        # 从 _execute_and_record 穿透（run_pick 透传回调异常）→ 退出码 2
        products = _execute_and_record(
            page, name, str(result["action"]), str(result["selector"]),
            result.get("columns"), out_dir, ledger_action="grab")
        picked["ok"] = products is not None

    try:
        picker.run_pick(page, _handler)  # 单条模式：不 repeat、不开会话
    except picker.PickCancelled as exc:
        print(f"已取消：{exc}")
        return 1
    return 0 if picked["ok"] else 1
