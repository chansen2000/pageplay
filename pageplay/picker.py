"""框选层：人在页面上圈表格/元素，产出正式 CSS 选择器与动作载荷。

流程（T7c 任务书契约）：run_pick 用 add_init_script 注入 overlay_js
（hover 高亮 + ↑ 扩选 / ↓ 收回 + Esc 取消 + 点击锁定；div 卡片列表按
"同签名兄弟 ≥3" 整组锁定，勾字段出 list_mode=cards 载荷）——每个新文档
加载时自动布防（跨页存活），当前已加载文档再补一次 evaluate；人确认
后 JS 调 window.__pageplay_onconfirm（expose_function 绑定跨文档持续
有效）→ python 收 payload → 用 COLLECT_CHAIN_JS 收集锁定元素到根的
链 → selector_from_chain 生成正式 selector → 组装并回调 on_confirm。
覆层 JS 自带心跳自愈（同文档内 SPA 剥 DOM/软跳转后自动重新布防）。
全程不改页面业务 DOM（只 append 覆层/面板，结束移除）。repeat=True
转会话常驻（T9a）：确认后清场重布防连续框选，人关窗 / Ctrl-C 才结束。

链序约定（selector_from_chain 与 COLLECT_CHAIN_JS 一致）：根在首、
目标元素在尾，与生成选择器的书写方向相同。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ._overlay_js import OVERLAY_JS

log = logging.getLogger(__name__)

__all__ = [
    "PickCancelled",
    "selector_from_chain",
    "overlay_js",
    "run_pick",
    "cover_screenshot",
    "COLLECT_CHAIN_JS",
]

_CHAIN_MAX = 5          # 选择器链长上限，超出保靠近元素侧
_PICK_TIMEOUT_SEC = 600  # 无人确认的兜底超时（人机交互，给足时间）

# 页面端收集脚本：window.__pageplay_locked（锁定元素，JS 点击时写入）
# 到根的链，返回 [{tag,id,classes,nth}]，根在首、锁定元素在尾。
# run_pick 内部使用；测试亦可直调（先自己写 window.__pageplay_locked）。
COLLECT_CHAIN_JS = """
() => {
  const el = window.__pageplay_locked;
  if (!el || el.nodeType !== 1) return [];
  function tagNth(n) {
    let k = 1, s = n;
    while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) k++;
    return k;
  }
  const chain = [];
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    chain.unshift({
      tag: n.tagName.toLowerCase(),
      id: n.id || null,
      classes: Array.prototype.slice.call(n.classList).filter(c => c),
      nth: tagNth(n),
    });
  }
  return chain;
}
"""


def selector_from_chain(chain: list[dict]) -> str:
    """元素链 → CSS 选择器（纯函数，规则同 JS 端 selector_hint）。

    chain 序：根在首、目标元素在尾；每环 {"tag","id","classes","nth"}
    （nth = 该 tag 在兄弟中的序号，1 起）。策略：

    - 链中任一环有 id（取最靠近目标的一个）→ 从该环起截断：该环渲染
      "#id"，前面环节丢弃
    - 无 id：全链拼接 tag + 首个 class；首环不带 nth，其余环 nth>1 才
      带 :nth-of-type(n)（nth-of-type(1) 与缺省同义，省略）
    - 链长上限 5 环，超出截头（保靠近元素侧）
    - 返回形如 "div.main > ul.list > li:nth-of-type(3) > a"

    空链 raise ValueError。
    """
    if not chain:
        raise ValueError("空链无法生成选择器：请回页面重新框选目标元素")
    links = [dict(link) for link in chain]
    cut = max((i for i, link in enumerate(links) if link.get("id")), default=-1)
    if cut >= 0:
        links = links[cut:]
    if len(links) > _CHAIN_MAX:
        links = links[len(links) - _CHAIN_MAX:]
    parts: list[str] = []
    for i, link in enumerate(links):
        head = i == 0
        if head and link.get("id"):
            parts.append("#" + str(link["id"]))
            continue
        part = str(link.get("tag", "*"))
        classes = [c for c in (link.get("classes") or []) if c]
        if classes:
            part += "." + str(classes[0])
        nth = link.get("nth", 1)
        if not head and nth and int(nth) > 1:
            part += f":nth-of-type({int(nth)})"
        parts.append(part)
    return " > ".join(parts)


def overlay_js() -> str:
    """返回注入 JS（IIFE，资产在 _overlay_js.OVERLAY_JS；契约见模块 docstring）。"""
    return OVERLAY_JS


class PickCancelled(RuntimeError):
    """人取消了拾取（Esc / 窗口关闭 / 超时无人确认 / Ctrl-C 且无已确认记录）。"""


# 当前拾取会话状态。expose_function 绑定随页面存活、不可换回调，故 shim
# 固定、经此路由到最近一次 run_pick（同页重复拾取时新会话覆盖旧会话）。
_ACTIVE: dict = {}


def _ensure_bindings(page) -> None:
    """向页面暴露确认/取消绑定（一次安装、跨文档持续有效）。

    Playwright 绑定随 page 注册、每个新文档自动可用，导航后无需也不能
    重装；同页第二次 run_pick 再装会抛 already registered——捕获后复用
    既有 shim 即可：回调固定经模块级 _ACTIVE 路由到最近一次 run_pick 的
    会话，新旧闭包语义完全一致。
    """

    def _on_confirm(payload: dict) -> None:
        state = _ACTIVE.get("state")
        if state is not None:
            state["payload"] = payload
            state["event"].set()

    def _on_cancel() -> None:
        state = _ACTIVE.get("state")
        if state is not None:
            state["cancelled"] = True
            state["event"].set()

    try:
        page.expose_function("__pageplay_onconfirm", _on_confirm)
        page.expose_function("__pageplay_oncancel", _on_cancel)
    except Exception as exc:
        if "already" not in str(exc).lower():
            raise  # 非重复安装类错误：如实上抛，不吞


def _rearm(page) -> None:
    """确认/Esc 后换防：重注全新覆层实例（IIFE 首行自清旧实例，幂等）。

    不抹 __pageplay_locked：on_confirm 长操作期间人可能已确认下一条
    （跨页时由 init script 自动布防），锁定元素要留给下一轮收集。
    页面恰在此刻被关则 evaluate 必失败——吞掉即可，随后的
    wait_for_timeout 会如实按"页面已关闭"收尾。
    """
    try:
        page.evaluate(overlay_js())
    except Exception:
        pass


def run_pick(page, on_confirm, repeat: bool = False) -> dict | list[dict]:
    """注入框选覆层等人确认，组装结果并回调 on_confirm。

    流程：add_init_script(overlay_js)——每个新文档加载时自动布防（跨页
    存活的关键），随后对当前已加载文档补一次 evaluate（同一份 JS 幂等；
    init script 不覆盖已加载页）→ 等待 JS 调 __pageplay_onconfirm（经
    expose 绑定收 payload，跨文档持续有效）→ page.evaluate
    (COLLECT_CHAIN_JS) 收集锁定元素到根的链 → selector_from_chain 生成
    正式 selector → 组装 {"selector","action","columns","list_mode",
    "fields","rect","url"}（卡片组确认时 list_mode="cards" + fields，
    selector 指向组父容器）→ 调 on_confirm(结果)（cli 落盘/确认打印钩子）。

    repeat=False（默认，T7c 行为不变）：一次确认 → 返回该条 dict；
    Esc / 窗口关闭 / 超时无人确认 raise PickCancelled（超时自进入拾取
    起算，跨页导航不重置）。

    repeat=True（T9a 会话常驻，"结束由人来定"）：每次确认 → 调
    on_confirm(payload) → 清场重布防继续框选 → 返回全部确认记录
    list[dict]（按序）。会话级退出条件（返回前都走 finally 清场）：
    - 浏览器窗口被用户关闭（既有 Page/Context closed 路径）：已收
      ≥1 条正常返回已收列表（不抛）；一条没收 → PickCancelled
    - 空闲超时：deadline 每轮确认后刷新（用户浏览多久都行，只要每
      600s 内有一次交互）；已收 ≥1 条正常返回并 log 说明；一条没收
      → PickCancelled
    - 终端 Ctrl-C（KeyboardInterrupt）：已收 ≥1 条正常返回；一条没收
      → PickCancelled
    Esc 语义不变：只取消当前锁定/面板，不退出会话（repeat 下清场重
    布防继续等）。on_confirm 抛异常不吞、向上透传（cli 执行失败要
    显示）。"已收"以 on_confirm 完整走完计，被打断的那条不算。

    url 语义 = 用户点确认那一刻的 location.href（跨页框选时即实际落点
    页 URL），与 cli 记账用的 page.url 同文档取值一致；payload 缺 url
    时兜底 page.url。返回前覆层已清理，页面业务 DOM 不留痕。
    """
    state = {"event": threading.Event(), "cancelled": False, "payload": None}
    _ACTIVE["state"] = state
    _ensure_bindings(page)
    results: list[dict] = []
    try:
        page.add_init_script(overlay_js())  # 新文档自动布防（跨页存活）
        page.evaluate(overlay_js())         # 当前文档补注入（幂等）
        deadline = time.monotonic() + _PICK_TIMEOUT_SEC
        while True:
            # 内层循环 = 等一条确认（repeat 的外层会话在 while True）
            while not state["event"].is_set():
                if time.monotonic() >= deadline:
                    if repeat and results:
                        log.info("拾取空闲超时（%ds 无交互），返回已确认的 %d 条",
                                 _PICK_TIMEOUT_SEC, len(results))
                        return results
                    raise PickCancelled(f"拾取超时（{_PICK_TIMEOUT_SEC}s 无人确认）")
                try:
                    # 等待期间 playwright 派发绑定回调（confirm/cancel 经此进入）
                    page.wait_for_timeout(100)
                except Exception as exc:
                    if repeat and results:
                        log.info("页面被关闭，返回已确认的 %d 条", len(results))
                        return results
                    raise PickCancelled(f"页面已关闭，拾取中止：{exc}") from exc
            if state["cancelled"]:
                if not repeat:
                    raise PickCancelled("人按 Esc 取消了框选")
                # repeat：Esc 只取消本轮锁定/面板，清场重布防继续等
                state["cancelled"] = False
                state["event"].clear()
                _rearm(page)
                deadline = time.monotonic() + _PICK_TIMEOUT_SEC
                continue
            payload = state["payload"] or {}
            chain = page.evaluate(COLLECT_CHAIN_JS)
            # 先复位握手再进 on_confirm：回调长操作期间人确认的下一条
            # 照常经绑定入队（event+payload+__pageplay_locked），不丢
            state["payload"] = None
            state["event"].clear()
            result = {
                "selector": selector_from_chain(chain),
                "action": payload.get("action"),
                "columns": payload.get("columns"),
                "list_mode": payload.get("list_mode") or "table",
                "fields": payload.get("fields"),  # 卡片组: [{label, rel}]；其余 None
                "rect": payload.get("rect"),
                "url": payload.get("url") or page.url,
            }
            on_confirm(result)
            results.append(result)
            if not repeat:
                return result
            _rearm(page)  # 确认后明确清场再布防，等下一条
            deadline = time.monotonic() + _PICK_TIMEOUT_SEC
    except KeyboardInterrupt:
        # 终端 Ctrl-C = 人结束会话：已收 ≥1 条正常返回，一条没收即取消
        if not results:
            raise PickCancelled("人 Ctrl-C 结束了框选（尚无已确认记录）")
        return results if repeat else results[-1]
    finally:
        _ACTIVE["state"] = None
        try:
            page.evaluate(
                "() => { if (window.__pageplay_cleanup) window.__pageplay_cleanup();"
                " window.__pageplay_locked = null; }")
        except Exception:
            pass  # 页面已关闭时清理必然失败，忽略（覆层随页面销毁）


def cover_screenshot(page, selector: str, out: Path) -> Path:
    """对选择器命中的元素截图到 out，返回 out。

    选择器未命中任何元素（或截图失败）raise ValueError 带指引——
    正常业务路径是回页面重新框选，而不是手改选择器。
    """
    out = Path(out)
    if page.locator(selector).count() == 0:
        raise ValueError(
            f"选择器 {selector!r} 未命中任何元素：页面结构可能已变化，"
            "请重新框选生成选择器")
    try:
        page.locator(selector).screenshot(path=str(out))
    except Exception as exc:
        raise ValueError(
            f"元素截图失败（选择器 {selector!r}）：{exc}；"
            "请重新框选或检查元素可见性") from exc
    return out
