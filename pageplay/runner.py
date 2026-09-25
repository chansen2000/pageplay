"""整链重放执行器（T11c）：按 flow 步骤序列在共享浏览器里逐步重放。

flow 结构（设计 §12，flows.py 数据层落盘）：{"name", "site", "url",
"steps": [{"no", "kind": "goto|click|table|download", "url",
"selector", "columns"}]}；"home_url"/"check_cookies" 可选（登录反弹
协作的落地页与登录标记，缺省按 _site_for_collab 回落）。
供 cli run（T11d 接线）调用：传入已附着的共享浏览器（CDP Browser，
用 contexts[0] 开页），全程只开一个页复用，结束关页、绝不关浏览器
（T10 常驻模型语义）。

步骤语义：
- goto：page.goto → 登录反弹检测（落点 host 含 "login."）→ 反弹则
  协作等人（ensure_logged_in：开 home_url 等人在窗口里过验证/登录，
  至多 120s 轮询），通过后回本步重试，上限 MAX_LOGIN_RETRY 次。
  语义对齐 cli_pick._goto_with_login_recovery——本模块自持一份等价
  实现，避免 runner 反向依赖 CLI 层（T11d 会从 cli 侧调进来，顶层
  互相 import 会成环）。
- click：等选择器（15s）→ 点击 → 等 1s 稳定。
- table：等选择器 → 按 list_mode 分派 extract_table（缺省）/
  extract_cards（"cards"，用 step["fields"]）→ save_table，产物名 <stem>-<no>。
- download：等选择器 → download_element 落盘。
每步后 check_page_risk：命中且不在登录页 → RiskTriggered 向上抛
（真风控照旧停机，退出码语义归调用方）；命中但落在登录页 = 中途被
弹回，按该步失败回报人话。任一步失败立即停（顺序语义），后续步不
执行；结果 {"ok", "failed_step", "results"} 向上回报。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import actions
from .guard import RiskTriggered
from .session import ensure_logged_in

log = logging.getLogger(__name__)

SELECTOR_TIMEOUT_MS = 15_000   # 选择器等待上限（同 cli_pick run 的 15s）
MAX_LOGIN_RETRY = 2            # 登录反弹协作重试上限（同 cli_pick run）

_LOGIN_BOUNCE_PREFIX = "登录态失效/被弹回登录页"
_LOGIN_WAIT_FAIL = (f"{_LOGIN_BOUNCE_PREFIX}，窗口里完成登录即可自动继续；"
                    "本次等待未通过，请重跑本命令")


class _StepFailed(Exception):
    """步骤失败（人话 detail 已定）：run_flow 捕获后记 fail 并停后续步。"""


# ----------------------------------------------------------------------
# 登录反弹检测与协作重试（语义对齐 cli_pick）
# ----------------------------------------------------------------------

def _is_login_bounce(url: str) -> bool:
    """登录反弹判定（设计 §11）：最终落点 host 含 "login." 即被弹回。"""
    return "login." in (urlsplit(url).hostname or "")


def _site_for_collab(flow: dict) -> SimpleNamespace:
    """从 flow 拼协作用的站点投影（ensure_logged_in 消费的字段）。

    协作落地页 home_url 回落链：flow["home_url"] → flow["url"] → 首个
    goto 步的 url（flows.py 数据层 REQUIRED 不含 home_url，回落保证
    goto 永远拿到合法 URL）；domain 取主机名最后两段（与
    sites.resolve_site 的注册域近似一致）；check_cookies 缺省空
    tuple（该域有任意 cookie 即算已登录，见 cookies.has_login_state）。
    """
    home_url = str(flow.get("home_url") or flow.get("url") or "")
    if not home_url:
        for step in flow.get("steps") or []:
            if step.get("kind") == "goto" and step.get("url"):
                home_url = str(step["url"])
                break
    hostname = urlsplit(home_url).hostname or ""
    domain = (".".join(hostname.split(".")[-2:])
              if "." in hostname else hostname)
    return SimpleNamespace(
        name=str(flow.get("site") or flow.get("name") or "flow"),
        home_url=home_url,
        domain=domain,
        check_cookies=tuple(flow.get("check_cookies") or ()),
    )


def _goto_with_login_recovery(page, browser, flow: dict, url: str) -> str | None:
    """goto 目标页；被弹回登录页 → 协作等人，通过后自动重试本步。

    每轮：goto → 反弹判定 → 未弹回再过 Guard（Guard 命中且落在登录页
    同样进协作；真风控照旧上抛）。弹回则 ensure_logged_in 等人（至多
    120s），通过后重试，上限 MAX_LOGIN_RETRY 次。成功返回 None；失败
    返回人话（等待未通过 / 重试耗尽）。
    """
    site = _site_for_collab(flow)
    for retry in range(MAX_LOGIN_RETRY + 1):
        page.goto(url)
        bounced = _is_login_bounce(page.url)
        if not bounced:
            try:
                actions.check_page_risk(page.content())
            except RiskTriggered as exc:
                if not _is_login_bounce(page.url):
                    raise  # 真风控（非登录页）：照旧向上抛停机
                log.info("run %s: Guard 命中（%s）且被弹回登录页，转风控协作",
                         url, exc)
                bounced = True
        if not bounced:
            return None
        if retry == MAX_LOGIN_RETRY:
            break
        log.info("run %s: 被弹回登录页（%s），进入风控协作", url, page.url)
        if not ensure_logged_in(browser, site):
            return _LOGIN_WAIT_FAIL
    return (f"{_LOGIN_BOUNCE_PREFIX}：窗口里完成登录即可自动继续"
            f"（已重试 {MAX_LOGIN_RETRY} 次仍未通过），请稍后重跑本命令")


# ----------------------------------------------------------------------
# 单步执行
# ----------------------------------------------------------------------

def _run_step(page, browser, flow: dict, step: dict, out_dir: Path,
              flow_stem: str) -> str:
    """执行单步，返回人话 detail；失败抛 _StepFailed / 超时照抛 / 真风控照抛。"""
    kind = str(step["kind"])
    if kind == "goto":
        detail = _goto_with_login_recovery(page, browser, flow, str(step["url"]))
        if detail is not None:
            raise _StepFailed(detail)
        return f"已打开 {page.url}"

    selector = str(step["selector"])
    page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT_MS)
    if kind == "click":
        page.click(selector)
        time.sleep(1.0)  # 等点击跳转/渲染稳定，下一步在落点上继续
        detail = f"已点击 {selector}，落点 {page.url}"
    elif kind == "table":
        if str(step.get("list_mode") or "table") == "cards":  # v0.7-A 卡片列表
            rows = actions.extract_cards(page, selector,
                                         step.get("fields") or [])
            what = "抓卡片"
        else:
            rows = actions.extract_table(page, selector, step.get("columns"))
            what = "抓表"
        csv_path, json_path = actions.save_table(
            rows, out_dir, stem=f"{flow_stem}-{step['no']}")
        detail = f"已生成：{what} {len(rows)} 行 → {csv_path}、{json_path}"
    elif kind == "download":
        target = actions.download_element(page, selector, out_dir)
        detail = (f"已生成 {target.resolve()}"
                  f"（{actions.file_size_str(target)}）")
    else:
        raise _StepFailed(f"第 {step['no']} 步动作类型不认识：{kind!r}"
                          "（只支持 goto/click/table/download）")

    # 每步后风控检查（照旧语义）：命中且不在登录页 → 真风控上抛；
    # 命中但落在登录页 = 流程中途被弹回，按该步失败回报
    try:
        actions.check_page_risk(page.content())
    except RiskTriggered as exc:
        if not _is_login_bounce(page.url):
            raise
        raise _StepFailed(f"{_LOGIN_BOUNCE_PREFIX}：流程中途被弹回登录页"
                          f"（{exc}）") from exc
    return detail


# ----------------------------------------------------------------------
# 整链入口
# ----------------------------------------------------------------------

def run_flow(browser, flow: dict, out_dir: Path, *, stem: str | None = None) -> dict:
    """按 step["no"] 序逐步重放 flow，返回执行结果。

    browser：已附着的共享浏览器（contexts[0] 开页；完事关页不关浏览器）。
    out_dir：产物目录（table/download 落盘处，不存在逐级创建）。
    stem：产物名前缀；缺省用 flow["name"]，再缺省 "flow"。
    返回 {"ok": bool, "failed_step": int|None, "results": [
        {"no", "kind", "status": "ok"|"fail", "detail": 人话}]}。
    """
    out_dir = Path(out_dir)
    steps = sorted(flow.get("steps") or [], key=lambda s: s["no"])
    flow_stem = str(stem or flow.get("name") or "flow")

    contexts = list(browser.contexts)
    if not contexts:
        raise RuntimeError(
            "常驻浏览器没有可用上下文；请执行 pageplay shutdown 后重试")
    page = contexts[0].new_page()

    results: list[dict] = []
    failed_step: int | None = None
    try:
        for step in steps:
            no, kind = step["no"], str(step["kind"])
            try:
                detail = _run_step(page, browser, flow, step, out_dir, flow_stem)
            except _StepFailed as exc:
                failed_step, detail = no, str(exc)
            except PlaywrightTimeoutError:
                failed_step = no
                if kind == "goto":
                    detail = (f"第 {no} 步打不开 {step.get('url', '')}："
                              "网络或页面加载超时")
                else:
                    detail = f"第 {no} 步卡住：页面结构可能变了"
            except RiskTriggered:
                raise  # 真风控：向上抛（调用方决定停机语义），已执行步保留
            except Exception as exc:  # 抓表列名不符/下载失败等：人话回报
                failed_step, detail = no, f"第 {no} 步失败：{exc}"
            results.append({"no": no, "kind": kind,
                            "status": "fail" if failed_step == no else "ok",
                            "detail": detail})
            if failed_step is not None:
                break  # 顺序语义：任一步失败即停，后续步不执行
    finally:
        try:
            page.close()
        except Exception:
            pass  # 页面可能已被关（人关窗等），浏览器本体不受影响
    return {"ok": failed_step is None, "failed_step": failed_step,
            "results": results}
