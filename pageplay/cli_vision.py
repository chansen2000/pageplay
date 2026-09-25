"""vision 命令：截屏把屏幕看到的表格转成表格（T21a，视觉第二路）。

与 grab 同款附着姿势：ensure_browser(headless=False) 附着活窗后由
pick_target_page 选「人正看着的可见标签页」（T19），不导航不开新页；
但不进框选交互——直接整页截图交视觉模型（vision.screenshot_table），
识别结果走 actions.save_table 落 CSV+JSON。是 DOM 框选之外的第二条
取数路：canvas 渲染、图片表格等 DOM 读不出的场景交给模型看。

站点参数可选，仅用于命名与落账归属（同 grab：不校验登录记录、不导航、
不校验当前页归属；缺省从当前页 URL 推导）。产物目录缺省
~/Downloads/pageplay/vision/（--out 可指到别处）。

GLM_API_KEY 未设 / 视觉服务异常时模型调用向上抛，由 main 统一映射
人话与退出码（配置问题 ValueError → 1；服务异常 RuntimeError → 1；
真风控 RiskTriggered → 2）。退出码：识别到表格并落盘 0；模型说没有
表格 1（fail 账，人话明示）；没有任何标签页 1。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import actions, vision
from .cli_grab import _label_from_page_url, _label_from_site_arg
from .cli_pick import _products_root, _record_run, _report_products
from .session import ensure_browser
from .target_page import pick_target_page


def register_vision(sub) -> None:
    """向 CLI 根解析器注册 vision 子命令（cli._build_parser 一行调用）。"""
    p = sub.add_parser(
        "vision", help="截屏当前页交视觉模型，把屏幕看到的表格转成表格")
    p.add_argument("site", nargs="?", default=None,
                   help="站点名或网址（可选，仅用于命名与落账归属）")
    p.add_argument("--out", default=None,
                   help="产物目录（默认 ~/Downloads/pageplay/vision）")
    p.set_defaults(func=_cmd_vision)


def _cmd_vision(args: argparse.Namespace) -> int:
    """vision：附着活窗取当前页，截屏交视觉模型识别，结果落盘落账。"""
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
        title = ""  # 个别内建页 title() 会失败，不让它挡识别
    site_arg = str(args.site or "").strip()  # 空白等价于没给
    label = (_label_from_site_arg(site_arg) if site_arg
             else _label_from_page_url(page.url))
    print(f"将截取该页画面交给视觉模型识别：{title or '（无标题）'}"
          f"（{page.url[:60]}）")

    rows = vision.screenshot_table(page)  # 缺 key/服务异常 → main 人话
    if not rows:
        print("✗ 视觉模型没有识别到表格"
              "（确认页面里真的有表格；没有表格的页不属于视觉抓取）")
        _record_run(f"{label}-vision", "vision", "fail",
                    "视觉模型没有识别到表格", [])
        return 1

    out_dir = (Path(args.out).expanduser() if args.out
               else _products_root() / "vision")
    stem = f"{label}-vision-{datetime.now():%Y%m%d-%H%M%S}"
    products = list(actions.save_table(rows, out_dir, stem=stem))
    _record_run(f"{label}-vision", "vision", "ok",
                _report_products("table", len(rows), products), products)
    print(f"✓ 视觉抓取 {len(rows)} 行 → "
          + "、".join(str(p.resolve()) for p in products))
    return 0
