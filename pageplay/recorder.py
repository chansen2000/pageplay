"""流程录制层（T11b）：人在活窗里自然浏览，click/框选自动记账成流程步骤。

流程（T11b 任务书契约 + 设计文档 §12）：record_session 用 add_init_script
注入 recorder_js（每个新文档自动布防，跨页存活——T8 经验），当前已加载
文档再补一次 evaluate。JS 侧 document 级 click capture 监听（passive，
不拦截不改变浏览手感）每记一次点击立即经 window.__pageplay_step 回传
（导航会杀 JS，不能攒着）；按 P（光标不在输入框时）置 picking 标记进
框选子模式，python 侧调 picker.run_pick 单条模式复用现有 overlay 全套
（picker.py 一字不动），确认产出收获步（table/download；卡片组确认把
list_mode/fields 原样透进步 dict，v0.7 收口）后清标记回浏览。
点击步落账后 3s 内的导航（framenavigated）把落点 URL 补进 note。

T16 页面反馈（盲操作治理）：录制布防时页面右上角 REC 角标（「● 录制中」
+ 可点的「框选」按钮 = 等价按 P）+ hover 1px 虚线轻高亮（outline 不改
布局不拦截，INPUT/TEXTAREA/角标/覆层豁免）+ 进框选顶部弹条（锁定或
取消随 picking 标记消失）。全部 append + 移除、不改业务 DOM，随会话
结束由 __pageplay_rec_cleanup 一并拆掉。

退出语义：关窗 / 空闲 600s 无交互 / Ctrl-C——已收 ≥1 步正常返回全部，
一条没收 raise PickCancelled。Esc 只取消当前框选回浏览模式，不结束会话。

首个框选结束后注册一个清场 init 脚本：此后每个新文档加载完立即拆掉
picker overlay 的自动布防——否则 run_pick 内部注册的 overlay init 脚本
会让后续每页首击被截获锁定，破坏"自然浏览"契约。
"""

from __future__ import annotations

import collections
import logging
import threading
import time

from . import picker
from .picker import PickCancelled

log = logging.getLogger(__name__)

__all__ = ["recorder_js", "merge_click_step", "record_session"]

_RECORD_IDLE_TIMEOUT_SEC = 600  # 空闲无交互兜底（人机交互，给足时间）
_NAV_MERGE_SEC = 3              # 点击步落账后等落点导航的窗口

# 注入 JS（IIFE，幂等单实例，复用 picker 的模式）：点击被动记录 + P 键
# 进框选子模式 + T16 页面反馈三件套（REC 角标 / hover 轻高亮 / 框选弹条）。
# picking 标记挂 window（python 确认/Esc 后清标记回浏览；测试也据此观测
# 子模式），新文档加载即回浏览态。
_RECORDER_JS = """
(() => {
  if (window.__pageplay_rec_cleanup) { try { window.__pageplay_rec_cleanup(); } catch (e) {} }
  window.__pageplay_picking = false;

  function tagNth(n) {
    let k = 1, s = n;
    while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) k++;
    return k;
  }
  function buildChain(el) {  // 根在首、目标在尾；与 picker COLLECT_CHAIN_JS 同规则
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
  function renderLink(l, isHead) {  // 与 python selector_from_chain 同规则
    if (isHead && l.id) return "#" + l.id;
    let s = l.tag;
    if (l.classes.length) s += "." + l.classes[0];
    if (!isHead && l.nth > 1) s += ":nth-of-type(" + l.nth + ")";
    return s;
  }
  function hint(el) {  // selector_hint：同 selector_from_chain 简化规则
    let chain = buildChain(el);
    let cut = -1;
    for (let i = 0; i < chain.length; i++) if (chain[i].id) cut = i;
    if (cut >= 0) chain = chain.slice(cut);
    if (chain.length > 5) chain = chain.slice(chain.length - 5);
    return chain.map((l, i) => renderLink(l, i === 0)).join(" > ");
  }
  function send(payload) {  // 即时回传（导航会杀 JS，不能攒着）
    try { if (window.__pageplay_step) window.__pageplay_step(payload); } catch (e) {}
  }
  function onClick(e) {  // passive capture：只记录，不拦截不改浏览手感
    if (window.__pageplay_picking) return;  // 框选子模式：点击归 overlay
    const t = e.target;
    if (!t || t.nodeType !== 1) return;
    if (t.closest && t.closest("#__pageplay_overlay")) return;  // 覆层 UI 不记
    if (t.closest && t.closest("#__pageplay_rec_badge")) return;  // 角标 UI 不记
    const a = t.closest ? t.closest("a[href]") : null;
    send({
      kind: "click",
      selector_hint: hint(t),
      href: a ? a.href : null,
      url: location.href,
    });
  }

  // ── T16 页面反馈三件套 ──
  // REC 角标（浏览态在，右上角，只有「框选」按钮可点）+ 框选弹条（子模式在，
  // 顶部居中）+ hover 轻高亮（1px 虚线 outline，不改布局不拦截）。三者全是
  // append + 移除、不改业务 DOM；角标/弹条显隐由 500ms 心跳同步 picking 标记
  // （覆盖 python 侧 _resume_browse 直接清标记的回浏览路径），进/出子模式时
  // 另有即时切换，不等人。
  let badge = null, banner = null, fbTimer = 0, hov = null, hovPrev = "";

  function hoverable(t) {  // 豁免：输入框 / 角标 / 弹条 / picker 覆层自身
    if (!t || t.nodeType !== 1) return false;
    if (t.tagName === "INPUT" || t.tagName === "TEXTAREA") return false;
    return !(t.closest && (t.closest("#__pageplay_rec_badge")
      || t.closest("#__pageplay_rec_banner")
      || t.closest("#__pageplay_overlay")));
  }
  function clearHov() {
    if (!hov) return;
    hov.style.outline = hovPrev;  // 还原行内 outline（原本没设是 ""，等于清除）
    hov = null; hovPrev = "";
  }
  function onOver(e) {
    if (window.__pageplay_picking || e.target === hov) return;
    clearHov();
    const t = e.target;
    if (!hoverable(t)) return;
    hov = t;
    hovPrev = t.style.outline || "";
    t.style.outline = "1px dashed #e5484d";
  }
  function onOut(e) { if (e.target === hov) clearHov(); }
  function syncFeedback() {  // 浏览态：角标在弹条藏；框选子模式：反之
    if (!badge || !banner) return;
    const picking = !!window.__pageplay_picking;
    badge.style.display = picking ? "none" : "flex";
    banner.style.display = picking ? "block" : "none";
  }
  function enterPick() {  // P 键与角标按钮共用的进框选入口（两者等价）
    if (window.__pageplay_picking) return;
    clearHov();  // 带着高亮进框选会与 overlay 红框打架，先进掉
    window.__pageplay_picking = true;
    syncFeedback();
    send({kind: "pick"});
  }
  function buildFeedback() {  // 角标+弹条挂 body（半透明不挡操作，仅按钮可点）
    if (!document.body) return;
    badge = document.createElement("div");
    badge.id = "__pageplay_rec_badge";
    badge.style.cssText = "position:fixed;top:8px;right:8px;z-index:2147483644;"
      + "display:flex;align-items:center;gap:5px;padding:2px 4px 2px 10px;"
      + "background:rgba(0,0,0,.45);border-radius:12px;color:#fff;"
      + "font:12px/1.8 -apple-system,sans-serif;pointer-events:none;opacity:.85;";
    badge.innerHTML = '<span style="color:#e5484d;">●</span><span>录制中</span>';
    const btn = document.createElement("button");
    btn.textContent = "框选";
    btn.style.cssText = "pointer-events:auto;cursor:pointer;color:#fff;"
      + "background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.7);"
      + "border-radius:10px;padding:0 9px;font:12px/1.8 -apple-system,sans-serif;";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      enterPick();
    });
    badge.appendChild(btn);
    document.body.appendChild(badge);
    banner = document.createElement("div");
    banner.id = "__pageplay_rec_banner";
    banner.textContent = "框选模式：红框罩住目标后单击锁定（Esc 返回浏览）";
    banner.style.cssText = "position:fixed;top:12px;left:50%;"
      + "transform:translateX(-50%);z-index:2147483644;display:none;"
      + "pointer-events:none;background:rgba(229,72,77,.92);color:#fff;"
      + "border-radius:6px;padding:5px 14px;"
      + "font:13px/1.8 -apple-system,sans-serif;";
    document.body.appendChild(banner);
    if (!fbTimer) fbTimer = setInterval(syncFeedback, 500);
    syncFeedback();
  }

  function onKey(e) {
    if (window.__pageplay_picking) return;  // 框选中按键归 picker overlay
    const ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
    if (e.key === "p" || e.key === "P") enterPick();
  }
  document.addEventListener("click", onClick, {capture: true, passive: true});
  document.addEventListener("keydown", onKey, {capture: true});
  document.addEventListener("mouseover", onOver, {capture: true});
  document.addEventListener("mouseout", onOut, {capture: true});
  if (document.readyState === "loading") {  // init 脚本先于 body：等 DOM 再挂
    document.addEventListener("DOMContentLoaded", buildFeedback, {once: true});
  } else {
    buildFeedback();
  }
  window.__pageplay_rec_cleanup = () => {
    document.removeEventListener("click", onClick, {capture: true});
    document.removeEventListener("keydown", onKey, {capture: true});
    document.removeEventListener("mouseover", onOver, {capture: true});
    document.removeEventListener("mouseout", onOut, {capture: true});
    clearInterval(fbTimer); fbTimer = 0;
    clearHov();
    if (badge) { badge.remove(); badge = null; }
    if (banner) { banner.remove(); banner = null; }
  };
})();
"""

# 首个框选结束后注册：每个新文档加载完立即拆掉 picker overlay 自动布防，
# 保住"自然浏览"（run_pick 内部的 overlay init 脚本会伴随每次导航重新布防）
_DISARM_PICKER_INIT = """
(() => {
  if (window.__pageplay_cleanup) { try { window.__pageplay_cleanup(); } catch (e) {} }
})();
"""

# 当前录制会话状态。expose_function 绑定随页面存活、不可换回调，shim 固定、
# 经此路由到最近一次 record_session（同页重复录制时新会话覆盖旧会话）。
_ACTIVE: dict = {}


def recorder_js() -> str:
    """返回注入 JS（IIFE，见 _RECORDER_JS；行为契约见模块 docstring）。"""
    return _RECORDER_JS


def merge_click_step(steps: list[dict], click_payload: dict,
                     final_url: str | None) -> list[dict]:
    """纯函数：click payload 归并为步骤 dict 追加到 steps，返回新列表（不改原表）。

    步骤形如 {"no","kind":"click","url"(点击时URL),"selector","note"}；
    note 记 href（链接跳转意图），final_url 给定且 ≠ 点击时 URL（确已
    换页）→ note 改记落点 URL（实际去向更权威）。no = len(steps)+1。
    """
    url = click_payload.get("url")
    note = click_payload.get("href") or ""
    if final_url and final_url != url:
        note = final_url
    step = {
        "no": len(steps) + 1,
        "kind": "click",
        "url": url,
        "selector": click_payload.get("selector_hint"),
        "note": note,
    }
    return list(steps) + [step]


def _ensure_step_binding(page) -> None:
    """向页面暴露 __pageplay_step 绑定（一次安装、跨文档持续有效）。

    同 picker._ensure_bindings：绑定不能重复安装，"already" 类错误复用
    既有 shim，回调经模块级 _ACTIVE 路由到最近一次 record_session。
    """

    def _on_step(payload: dict) -> None:
        state = _ACTIVE.get("state")
        if state is not None:
            state["queue"].append(dict(payload or {}))
            state["event"].set()

    try:
        page.expose_function("__pageplay_step", _on_step)
    except Exception as exc:
        if "already" not in str(exc).lower():
            raise  # 非重复安装类错误：如实上抛，不吞


def _finish(steps: list[dict], reason: str) -> list[dict]:
    """会话收尾：已收 ≥1 步正常返回全部；一条没收 raise PickCancelled。"""
    if not steps:
        raise PickCancelled(f"{reason}（尚无任何步骤）")
    log.info("%s，返回已记录的 %d 步", reason, len(steps))
    return steps


def _resume_browse(page) -> bool:
    """清 picking 标记回浏览模式。页面已关则 False（交主循环按关窗收尾）。"""
    try:
        page.evaluate("() => { window.__pageplay_picking = false; }")
        return True
    except Exception:
        return False


def _enter_pick(page, steps: list[dict], on_harvest) -> None:
    """P 进框选子模式：picker.run_pick 单条模式（现有契约，picker.py 不动）。

    on_harvest 收到与 on_step 同构的流程步骤 dict（kind=table/download，
    框选 payload 的 action 折进 kind）。卡片组确认（list_mode=cards）把
    list_mode/fields 原样透进步 dict（runner 靠它们走 extract_cards，
    缺了重放必坏）；纯表格步不带这两键（向后兼容，v0.7 收口）。
    Esc/超时 raise PickCancelled：
    页面还活着 → 回浏览模式继续录（不刷新空闲时限，Esc/超时不算进展）；
    页面已关 → 向上抛给主循环按关窗语义收尾。on_harvest 抛异常不吞、
    经 run_pick 向上透传（cli 执行失败要显示）。
    """

    def _on_confirm(result: dict) -> None:
        step = {
            "no": len(steps) + 1,
            "kind": result.get("action"),
            "url": result.get("url"),
            "selector": result.get("selector"),
            "columns": result.get("columns"),
            "note": "",
        }
        if result.get("list_mode") == "cards":  # 卡片确认：字段清单透传（重放必需）
            step["list_mode"] = "cards"
            step["fields"] = result.get("fields")
        steps.append(step)
        on_harvest(step)

    try:
        picker.run_pick(page, _on_confirm)
    except PickCancelled:
        if not _resume_browse(page):
            raise  # 页面已关闭：主循环按关窗语义收尾
        log.info("框选已取消，回浏览模式继续录制")
        return
    # 确认成功：清标记回浏览 + 注册清场 init（此后新文档不再自动布防
    # picker overlay，保住自然浏览；重复注册幂等无害）
    if not _resume_browse(page):
        return
    try:
        page.add_init_script(_DISARM_PICKER_INIT)
    except Exception:
        pass  # 页面恰在此刻被关：主循环按关窗语义收尾


def _await_landing(page, state: dict, step: dict) -> None:
    """点击步落账后 3s 窗口内补落点：确已换页 → note 记落点 URL。

    窗口内来了新交互则提前收窗（先补已发生的落点再处理新交互）；
    页面关闭静默返回（主循环按关窗语义收尾）。
    """
    end = time.monotonic() + _NAV_MERGE_SEC
    while time.monotonic() < end and not state["event"].is_set():
        try:
            page.wait_for_timeout(50)
        except Exception:
            return
    try:
        final_url = page.url
    except Exception:
        return
    if final_url and final_url != step["url"]:
        step["note"] = final_url


def record_session(page, on_step, on_harvest) -> list[dict]:
    """录制会话主循环：注入 recorder_js → 收 click/框选 → 混排步骤列表。

    流程：add_init_script(recorder_js)——每个新文档自动布防（跨页存活），
    当前已加载文档补一次 evaluate（T8 经验）→ 循环收 __pageplay_step：
    click → merge_click_step 归账并调 on_step(step)（cli 回显钩子，异常
    向上透传）→ 3s 内补落点；P → _enter_pick 框选子模式，收获步调
    on_harvest(step)。每次交互刷新空闲时限；返回有序步骤 list[dict]
    （click 与 harvest 混排，kind 区分）。

    退出（都走 finally 拆监听收账）：
    - 关窗：已收 ≥1 步正常返回全部；一条没收 raise PickCancelled
    - 空闲 600s 无交互：同关窗语义，log 说明
    - Ctrl-C（KeyboardInterrupt）：已收 ≥1 步正常返回；一条没收
      raise PickCancelled
    """
    state = {"event": threading.Event(), "queue": collections.deque()}
    _ACTIVE["state"] = state
    _ensure_step_binding(page)
    steps: list[dict] = []
    try:
        page.add_init_script(recorder_js())  # 新文档自动布防（跨页存活）
        page.evaluate(recorder_js())         # 当前文档补注入（幂等）
        deadline = time.monotonic() + _RECORD_IDLE_TIMEOUT_SEC
        while True:
            # 排空队列（FIFO 保序）：先来的交互先记账
            while state["queue"]:
                item = state["queue"].popleft()
                deadline = time.monotonic() + _RECORD_IDLE_TIMEOUT_SEC
                kind = item.get("kind")
                if kind == "pick":
                    _enter_pick(page, steps, on_harvest)
                elif kind == "click":
                    steps = merge_click_step(steps, item, None)
                    on_step(steps[-1])
                    _await_landing(page, state, steps[-1])
                else:
                    log.warning("录制收到未知交互 kind=%r，忽略", kind)
            # 等下一条交互
            while not state["event"].is_set():
                if time.monotonic() >= deadline:
                    return _finish(
                        steps, f"录制空闲超时（{_RECORD_IDLE_TIMEOUT_SEC}s 无交互）")
                try:
                    # 等待期间 playwright 派发绑定回调（__pageplay_step 经此进入）
                    page.wait_for_timeout(100)
                except Exception as exc:
                    return _finish(steps, f"页面已关闭，录制结束：{exc}")
            state["event"].clear()
    except KeyboardInterrupt:
        # 终端 Ctrl-C = 人结束录制：已收 ≥1 步正常返回，一条没收即取消
        return _finish(steps, "人 Ctrl-C 结束了录制")
    finally:
        _ACTIVE["state"] = None
        try:
            page.evaluate(
                "() => { if (window.__pageplay_rec_cleanup)"
                " window.__pageplay_rec_cleanup(); }")
        except Exception:
            pass  # 页面已关闭时清理必然失败，忽略（监听随页面销毁）
