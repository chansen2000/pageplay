"""pick / run / record / results 命令处理：确认即执行、流程录制与整链
重放、风控协作、执行回看。

cli.py 在 _build_parser 内延迟 import 本模块（避免顶层循环依赖），这里
反向只绑定 cli 模块对象（`from . import cli as _cli`）：共享底层（站点
解析、sites 根路径）一律经 _cli.属性在调用期取用。常驻浏览器经
session 模块（ensure_browser / ensure_headful_browser / ensure_logged_in）
附着——命令只开页干活、完事关页，绝不关浏览器。

确认即执行：pick 每确认一条，confirm_and_execute 一次完成「存 recipe →
当场执行 → 打印 ✓/✗ → 执行账本落账 → 顺手截封面」，单条失败不中断
框选会话；pick 以 repeat=True 跑会话（T9a/T9c），结束打印摘要。record
（T11d）录制整段浏览为流程：收获步当场执行落账但不存单条 recipe，结束
起名存 flow（flows.py）。run 双查：名字先查流程（runner.run_flow 整链
重放），未命中再查 recipe（单条重放，原逻辑不变）。重放遇登录反弹不
直接停：进风控协作（ensure_logged_in）等人在窗口里完成登录/验证，通过
后自动续跑（重试上限 2 次）；results 读账本回看。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import (actions, cli as _cli, flows, picker, recipes, recorder, runs,
               runner)
from .guard import RiskTriggered
from .session import ensure_browser, ensure_headful_browser, ensure_logged_in

log = logging.getLogger(__name__)

# download 执行超时的人话：点击后迟迟没有下载事件，多半框错了元素类型
_DOWNLOAD_NO_FILE_HINT = "点击后没有触发文件下载——选的可能不是下载类元素"

_LOGIN_BOUNCE_PREFIX = "登录态失效/被弹回登录页"


# ----------------------------------------------------------------------
# 执行账本与产物目录
# ----------------------------------------------------------------------

def _runs_path() -> Path:
    """执行账本路径：PAGEPLAY_HOME 覆盖，默认 ~/.pageplay/runs.jsonl
    （与 sites 根同级，不进站点目录——账本跨站统一一份）。"""
    return Path(os.environ.get("PAGEPLAY_HOME", "~/.pageplay")).expanduser() / "runs.jsonl"


def _products_root() -> Path:
    """产物根目录：~/Downloads/pageplay/（每个 recipe 一个子目录）。"""
    return Path.home() / "Downloads" / "pageplay"


def _default_out_dir(name: str) -> Path:
    """产物目录缺省：<产物根>/<recipe名>/（pick 即时执行与 run 一致）。"""
    return _products_root() / name


def _record_run(name: str, action: str, status: str, detail: str,
                outputs: list[Path]) -> None:
    """执行结果落账；账本写入失败只提示一句，不影响已完成的执行本身。"""
    entry = {"recipe": name, "action": action, "status": status,
             "detail": detail, "outputs": [str(p) for p in outputs]}
    try:
        runs.record_run(_runs_path(), entry)
    except (OSError, ValueError) as exc:
        print(f"（执行记录写入失败：{exc}）", file=sys.stderr)


# ----------------------------------------------------------------------
# 单次执行（pick 确认即执行与 run 重放共用）
# ----------------------------------------------------------------------

def _execute_recipe(page, recipe: dict, out_dir: Path) -> tuple[int, list[Path]]:
    """按 recipe 在当前页执行一次动作，返回 (行数, 产物路径)。

    table：抽表（columns 过滤）→ CSV+JSON 双份落盘（文件名带时间戳）；
    download：点击触发下载、按浏览器建议文件名落盘。异常向上抛，由
    调用方负责 ✗ 展示与落账。
    """
    out_dir = Path(out_dir)
    if recipe["action"] == "table":
        rows = actions.extract_table(page, recipe["selector"],
                                     recipe.get("columns"))
        stem = f"{recipe['name']}-{datetime.now():%Y%m%d-%H%M%S}"
        return len(rows), list(actions.save_table(rows, out_dir, stem=stem))
    return 0, [actions.download_element(page, recipe["selector"], out_dir)]


def _report_products(action: str, rows: int, products: list[Path]) -> str:
    """拼一条人话结果：行数（表）+ 每个产物的绝对路径与大小（无 ✓ 前缀）。"""
    items = "、".join(f"{p.resolve()}（{actions.file_size_str(p)}）"
                      for p in products)
    if action == "table":
        return f"已生成：抓表 {rows} 行 → {items}"
    return f"已生成 {items}"


def _save_cover(page, result: dict, site_dir: Path, name: str) -> None:
    """确认后顺手给 recipe 截封面（best-effort：失败只 log，不打断会话）。"""
    try:
        picker.cover_screenshot(page, result["selector"],
                                site_dir / "recipes" / f"{name}.png")
    except Exception as exc:
        log.warning("封面截图失败（%s）：%s", name, exc)


def _execute_and_record(page, name: str, action: str, selector: str,
                        columns, out_dir: Path) -> list[Path] | None:
    """当场执行一次动作并落账（pick 确认执行与 record 收获执行共用）。

    执行 → 打印 ✓（产物绝对路径+大小）或 ✗（人话原因）→ 结果无论成败
    落账 runs.jsonl。执行失败不向上抛：单条失败不打断框选/录制会话；
    成功返回产物路径列表，失败返回 None（调用方据此决定是否截封面）。
    """
    recipe = {"name": name, "action": action, "selector": selector,
              "columns": columns}
    try:
        rows, products = _execute_recipe(page, recipe, out_dir)
    except PlaywrightTimeoutError:
        detail, products = _DOWNLOAD_NO_FILE_HINT, None
    except Exception as exc:  # 抓表/下载/落盘任何失败：人话 ✗，会话继续
        detail, products = str(exc), None
    if products is not None:
        line = _report_products(action, rows, products)
        print(f"✓ {line}")
        _record_run(name, action, "ok", line, products)
        return products
    print(f"✗ {detail}", file=sys.stderr)
    _record_run(name, action, "fail", detail, [])
    return None


def confirm_and_execute(page, site, site_dir: Path, name: str,
                        result: dict) -> None:
    """单条确认的完整闭环（pick 的 on_confirm 钩子本体，可逐条复用）。

    存 recipe → 当场执行一次（_execute_and_record：✓/✗ + 落账）→ 成功
    再顺手截封面。任何一步失败都不向上抛：单条失败只打 ✗ 并记 fail 账，
    框选会话继续。
    """
    print(f"已锁定 {result['action']}：{result['selector']}")
    try:
        recipe = {"version": 1, "name": name, "site": site.name,
                  "url": page.url, "action": result["action"],
                  "selector": result["selector"], "columns": result["columns"],
                  "screenshot": f"{name}.png"}
        recipes.save_recipe(site_dir, recipe)
    except Exception as exc:
        detail = f"recipe 保存失败：{exc}"
        print(f"✗ {detail}", file=sys.stderr)
        _record_run(name, str(result["action"]), "fail", detail, [])
        return
    products = _execute_and_record(page, name, recipe["action"],
                                   recipe["selector"], recipe["columns"],
                                   _default_out_dir(name))
    if products is not None:
        _save_cover(page, result, site_dir, name)


# ----------------------------------------------------------------------
# 风控协作重放（run 的 goto + 登录反弹处理）
# ----------------------------------------------------------------------

def _is_login_bounce(url: str) -> bool:
    """登录反弹判定（设计 §11）：最终落点 host 含 "login." 即被弹回登录页。"""
    return "login." in (urlsplit(url).hostname or "")


def _goto_with_login_recovery(page, browser, site, url: str,
                              max_retries: int = 2) -> str | None:
    """goto 目标页；被弹回登录页 → 风控协作等人，通过后自动续跑。

    每轮：goto → 反弹判定（host 含 login.）→ 未弹回再过 Guard（Guard
    命中且落在登录页同样进协作，风控关键词只作 log 详情；真风控照旧
    上抛停机）。弹回则开首页等人在窗口里完成登录/验证（至多 120s），
    通过后重试，上限 max_retries 次。成功返回 None；失败返回人话。
    """
    for retry in range(max_retries + 1):
        page.goto(url)
        bounced = _is_login_bounce(page.url)
        if not bounced:
            try:
                actions.check_page_risk(page.content())
            except RiskTriggered as exc:
                if not _is_login_bounce(page.url):
                    raise  # 真风控（非登录页）：照旧停机（退出码 2）
                log.info("run %s: Guard 命中（%s）且被弹回登录页，转风控协作",
                         url, exc)
                bounced = True
        if not bounced:
            return None
        if retry == max_retries:
            break
        log.info("run %s: 被弹回登录页（%s），进入风控协作", url, page.url)
        if not ensure_logged_in(browser, site):
            return (f"{_LOGIN_BOUNCE_PREFIX}，窗口里完成登录即可自动继续；"
                    "本次等待未通过，请重跑本命令")
    return (f"{_LOGIN_BOUNCE_PREFIX}：窗口里完成登录即可自动继续"
            f"（已重试 {max_retries} 次仍未通过），请稍后重跑本命令")


# ----------------------------------------------------------------------
# pick / run / results 三命令
# ----------------------------------------------------------------------

def _cmd_pick(args: argparse.Namespace) -> int:
    """pick：附着有头活窗连续框选（repeat 会话），每条确认即执行。

    第 1 条用 --name（缺省 <站点>-<序号>），第 2 条起自动 <名>-2、<名>-3…
    互不覆盖；每条确认走 confirm_and_execute（存+执行+落账+封面）。
    会话结束（关窗/空闲超时/Ctrl-C）打印摘要：收了几条、名字列表、
    产物目录；一条没收（Esc/关窗）→ 已取消，退出码 1。
    """
    try:
        _name, pasted_url = _cli.parse_target(args.site)
        site = _cli._resolve_target(args.site)
    except (KeyError, ValueError) as exc:
        print(f"站点解析失败：{exc}", file=sys.stderr)
        return 1
    site_dir = _cli._site_dir(site.name)
    base_name = args.name or f"{site.name}-{len(recipes.list_recipes(site_dir)) + 1}"

    browser = ensure_headful_browser()
    context = browser.contexts[0]
    page = context.new_page()
    names: list[str] = []
    counter = [0]

    def _handler(result: dict) -> None:
        counter[0] += 1
        name = base_name if counter[0] == 1 else f"{base_name}-{counter[0]}"
        names.append(name)
        confirm_and_execute(page, site, site_dir, name, result)

    try:
        page.goto(pasted_url or site.home_url)
        picker.run_pick(page, _handler, repeat=True)
    except picker.PickCancelled as exc:
        print(f"已取消：{exc}")
        return 1
    finally:
        try:
            page.close()
        except Exception:
            pass  # 人关窗收摊时页面已没了
    print(f"本次框选会话结束：共收 {len(names)} 条")
    for name in names:
        print(f"  {name} → 以后一条命令重放：pageplay run {name}")
    print(f"产物目录：{_products_root()}")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    """record：附着有头活窗录制整段浏览为流程（T11d，设计 §12）。

    自然浏览：点击照常放行、自动记账（recorder.py 契约，cli 不重复造）；
    按 P 进框选产生收获步（table/download），当场执行并落账——不存单条
    recipe，流程才是归属。结束（关窗/空闲超时/Ctrl-C）给流程起名
    （--name 直用；回车默认 <站>-flow-N）落盘 flows/<名字>.json，以后
    pageplay run <名字> 整链重放。录制期产物放占位目录（流程名结束才
    定）。一条步骤没收（PickCancelled）→ 已取消，退出码 1。
    """
    try:
        _name, pasted_url = _cli.parse_target(args.site)
        site = _cli._resolve_target(args.site)
    except (KeyError, ValueError) as exc:
        print(f"站点解析失败：{exc}", file=sys.stderr)
        return 1
    site_dir = _cli._site_dir(site.name)
    draft_name = f"{site.name}-flow"  # 录制期占位名（流程名结束才定）
    draft_dir = _default_out_dir(draft_name) / "record"

    browser = ensure_headful_browser()
    context = browser.contexts[0]
    page = context.new_page()
    steps: list[dict] = []
    start_url = ""
    try:
        detail = _goto_with_login_recovery(
            page, browser, site, pasted_url or site.home_url)
        if detail is not None:
            print(f"✗ {detail}", file=sys.stderr)
            return 1
        start_url = page.url

        def _on_step(step: dict) -> None:
            print(f"已记步骤 {step['no']}：{step['kind']} {step['selector']}")

        def _on_harvest(step: dict) -> None:
            _execute_and_record(page, draft_name, str(step["kind"]),
                                str(step["selector"]), step.get("columns"),
                                draft_dir)

        steps = recorder.record_session(page, _on_step, _on_harvest)
    except picker.PickCancelled as exc:
        print(f"已取消：{exc}")
        return 1
    finally:
        try:
            page.close()
        except Exception:
            pass  # 人关窗收摊时页面已没了
    if not steps:
        print("未记录任何操作")
        return 1

    default_name = flows.next_flow_name(site_dir, site.name)
    if args.name:
        flow_name = str(args.name)
    else:
        try:
            flow_name = (input(f"给这次流程起个名（回车 = {default_name} 默认）：")
                         .strip() or default_name)
        except EOFError:
            print("未输入流程名，本次录制未保存（上面已有步骤与产物）",
                  file=sys.stderr)
            return 1
    try:
        flows.save_flow(site_dir, {"version": 1, "name": flow_name,
                                   "site": site.name, "url": start_url,
                                   "steps": steps})
    except ValueError as exc:
        print(f"流程保存失败：{exc}", file=sys.stderr)
        return 1
    harvested = sum(1 for s in steps if s.get("kind") in ("table", "download"))
    print(f"流程已保存：{flow_name}")
    print(f"  共 {len(steps)} 步（收获步 {harvested} 个）｜起始页 {start_url}")
    print(f"  重放整段：pageplay run {flow_name}")
    print(f"  录制期产物目录：{draft_dir}")
    return 0


def _snapshot_dir_files(out_dir: Path) -> dict[str, tuple[int, int]]:
    """产物目录现存文件快照 {路径: (大小, mtime_ns)}；目录不存在 = 空表。"""
    if not out_dir.is_dir():
        return {}
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in out_dir.rglob("*") if p.is_file()}


def _run_flow_by_name(args: argparse.Namespace, flow_dir: Path) -> int:
    """流程整链重放：runner.run_flow 逐步执行，一条 run 账 + ✓/✗ 人话。

    产物目录缺省 ~/Downloads/pageplay/<流程名>/（--out 覆盖）；产出清单
    用"执行前后目录快照对比"得出（runner 只回人话 detail，结构化产物
    归调用方点数）。登录反弹协作、逐步重试语义全在 runner 内部。
    """
    name = str(args.name)
    flow = flows.load_flow(flow_dir, name)
    out_dir = (Path(args.out).expanduser() if args.out
               else _default_out_dir(name))
    before = _snapshot_dir_files(out_dir)
    browser = ensure_browser(headless=args.headless)
    result = runner.run_flow(browser, flow, out_dir, stem=name)
    outputs = sorted(Path(p) for p, sig in _snapshot_dir_files(out_dir).items()
                     if before.get(p) != sig)
    steps_line = "；".join(
        f"第{r['no']}步[{r['kind']}]{'✓' if r['status'] == 'ok' else '✗'}："
        f"{r['detail']}" for r in result["results"])
    if result["ok"]:
        detail = f"{len(result['results'])} 步全部完成：{steps_line}"
        print(f"✓ 流程 {name}：{len(result['results'])} 步全部完成")
        for p in outputs:
            print(f"  产物 {p.resolve()}（{actions.file_size_str(p)}）")
        _record_run(name, "flow", "ok", detail, outputs)
        return 0
    failed = next((r for r in result["results"] if r["status"] == "fail"),
                  {"no": result["failed_step"], "detail": "执行中断"})
    print(f"✗ 流程 {name} 第 {failed['no']} 步：{failed['detail']}",
          file=sys.stderr)
    _record_run(name, "flow", "fail", steps_line, outputs)
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    """run：重放流程或 recipe（双查：名字先查 flows 整链，再查 recipe）。

    流程命中 → runner.run_flow 整链自动重放（默认附着有头活窗，
    --headless 走无头守护），整链一条 run 账。recipe 命中 → 原单条重放
    逻辑原样（T9b 语义零回归）。被弹回登录页都不直接停：进风控协作等人
    在窗口里完成登录/验证，通过后自动续跑（重试上限 2 次）。两者都找
    不到 → 人话列出可用流程与 recipe；结果无论成败落账 runs.jsonl。
    """
    flow_dir = next((d for d in _cli._flow_site_dirs()
                     if (d / "flows" / f"{args.name}.json").is_file()), None)
    if flow_dir is not None:
        return _run_flow_by_name(args, flow_dir)
    site_dir = next((d for d in _cli._recipe_site_dirs()
                     if (d / "recipes" / f"{args.name}.json").is_file()), None)
    if site_dir is None:
        flow_names = sorted(str(f["name"]) for d in _cli._flow_site_dirs()
                            for f in flows.list_flows(d))
        recipe_names = sorted(str(r["name"]) for d in _cli._recipe_site_dirs()
                              for r in recipes.list_recipes(d))
        flow_known = "、".join(flow_names) if flow_names else "（一个都没有）"
        recipe_known = "、".join(recipe_names) if recipe_names else "（一个都没有）"
        print(f"{args.name!r} 既不是已保存的流程，也不是 recipe；"
              f"现有流程：{flow_known}；现有 recipe：{recipe_known}",
              file=sys.stderr)
        return 1
    recipe = recipes.load_recipe(site_dir, args.name)
    try:
        site = _cli._resolve_target(recipe["site"])
    except KeyError as exc:
        print(f"recipe 所属站点解析失败：{exc}", file=sys.stderr)
        return 1
    out_dir = (Path(args.out).expanduser() if args.out
               else _default_out_dir(recipe["name"]))
    browser = ensure_browser(headless=args.headless)
    context = browser.contexts[0]
    page = context.new_page()
    status, detail, products = "fail", "", []
    try:
        detail = _goto_with_login_recovery(page, browser, site, recipe["url"])
        if detail is None:
            try:
                page.wait_for_selector(recipe["selector"], timeout=15000)
            except PlaywrightTimeoutError:
                detail = f"页面结构可能变了，请重新 pick {site.name}"
            else:
                try:
                    rows, products = _execute_recipe(page, recipe, out_dir)
                except PlaywrightTimeoutError:
                    detail = _DOWNLOAD_NO_FILE_HINT
                except Exception as exc:
                    detail = str(exc)
                else:
                    status = "ok"
                    detail = _report_products(recipe["action"], rows, products)
                    products = list(products)
    finally:
        try:
            page.close()
        except Exception:
            pass  # 页面可能已被关（守护退出等），浏览器本体不受影响
    if status == "ok":
        print(f"✓ {detail}")
        _record_run(recipe["name"], recipe["action"], "ok", detail, products)
        return 0
    print(f"✗ {detail}", file=sys.stderr)
    _record_run(recipe["name"], recipe["action"], "fail", detail, [])
    return 1


def _cmd_results(args: argparse.Namespace) -> int:
    """results：回看执行账本（新在前）；带 recipe 名只看该 recipe。

    每行：时间 / recipe / 动作 / ✓✗ / 产物路径（成功）或失败原因（失败）。
    只读视图，永远退出码 0。
    """
    entries = runs.load_runs(_runs_path(), recipe=args.recipe, limit=args.limit)
    if not entries:
        target = f"recipe {args.recipe!r} " if args.recipe else ""
        print(f"{target}暂无执行记录。pick 确认或 pageplay run 执行后可在这里回看。")
        return 0
    width = max(len(str(e["recipe"])) for e in entries)
    print(f"执行记录（最近 {len(entries)} 条，新在前）：")
    for e in entries:
        mark = "✓" if e.get("status") == "ok" else "✗"
        outcome = "、".join(str(o) for o in e.get("outputs") or [])
        if not outcome:
            outcome = str(e.get("detail", ""))
        print(f"  {e.get('created_at', '?')}  {str(e['recipe']):<{width}}  "
              f"{str(e['action']):<8}  {mark}  {outcome}")
    return 0
