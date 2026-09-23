"""框选层：人在页面上圈表格/元素，产出正式 CSS 选择器与动作载荷。

流程（T7c 任务书契约）：run_pick 用 add_init_script 注入 overlay_js
（hover 高亮 + ↑ 扩选 / ↓ 收回 + Esc 取消 + 点击锁定）——每个新文档
加载时自动布防（跨页存活），当前已加载文档再补一次 evaluate；人确认
后 JS 调 window.__pageplay_onconfirm（expose_function 绑定跨文档持续
有效）→ python 收 payload → 用 COLLECT_CHAIN_JS 收集锁定元素到根的
链 → selector_from_chain 生成正式 selector → 组装返回。覆层 JS 自带
心跳自愈（同文档内 SPA 剥 DOM/软跳转后自动重新布防）。全程不改页面
业务 DOM（只 append 覆层/面板，结束移除）。

链序约定（selector_from_chain 与 COLLECT_CHAIN_JS 一致）：根在首、
目标元素在尾，与生成选择器的书写方向相同。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

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

# 注入 JS（IIFE）：hover 高亮 + 键盘扩收 + Esc 取消 + 点击锁定/列勾选。
# 只 append 覆层（#__pageplay_overlay），不改页面业务 DOM；
# 结束（确认/取消/清理）由 __pageplay_cleanup 移除全部覆层与监听。
# 经 add_init_script 注入时每个新文档自动布防；同文档内覆层被页面剥掉
# （SPA 软跳转/DOM 重建）由心跳自愈重新布防，全程幂等、单实例。
_OVERLAY_JS = """
(() => {
  if (window.__pageplay_cleanup) { try { window.__pageplay_cleanup(); } catch (e) {} }

  const SEM = "table,ul,ol,[role=grid]";
  const HB_MS = 800;  // 自愈心跳周期：覆层被剥后最迟一个周期重新布防
  let current = null;   // 当前高亮目标
  let locked = null;    // 点击锁定后的目标
  const upStack = [];   // ↑ 扩选记录（↓ 收回用）
  let overlay = null, box = null, panel = null;
  let ac = null;        // 文档监听生命周期（abort = 监听已拆）
  let hbTimer = 0;
  let over = false;     // 会话结束（确认/取消/清理）：不再布防

  function semantic(el) {
    try { return (el && el.closest && el.closest(SEM)) || el; } catch (e) { return el; }
  }
  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  }
  function moveBox(el) {
    const r = rectOf(el);
    box.style.left = r.x + "px";
    box.style.top = r.y + "px";
    box.style.width = r.w + "px";
    box.style.height = r.h + "px";
    box.style.display = "block";
  }
  function tagNth(n) {
    let k = 1, s = n;
    while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) k++;
    return k;
  }
  function buildChain(el) {  // 根在首、目标在尾；与 python COLLECT_CHAIN_JS 同规则
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
  function buildUi() {  // 建覆层三件套并挂到 body（重建时同步重置拾取态）
    current = null; locked = null; upStack.length = 0;
    overlay = document.createElement("div");
    overlay.id = "__pageplay_overlay";
    box = document.createElement("div");
    box.style.cssText = "position:fixed;display:none;pointer-events:none;z-index:2147483646;"
      + "border:2px solid #e5484d;background:rgba(229,72,77,.10);box-sizing:border-box;";
    panel = document.createElement("div");
    panel.style.cssText = "position:fixed;top:12px;right:12px;display:none;z-index:2147483647;"
      + "background:#fff;border:1px solid #ccc;border-radius:6px;padding:10px 12px;"
      + "font:13px/1.6 -apple-system,sans-serif;color:#222;"
      + "box-shadow:0 4px 16px rgba(0,0,0,.15);max-height:70vh;overflow:auto;";
    overlay.appendChild(box);
    overlay.appendChild(panel);
    (document.body || document.documentElement).appendChild(overlay);
  }
  function alive() {  // 覆层三件套仍全部挂在文档里
    return !!(overlay && box && panel && overlay.isConnected
      && box.isConnected && panel.isConnected);
  }
  function confirmPick(action, columns) {
    if (!locked) return;
    over = true;  // 会话结束：停自愈，UI 留给 python 收尾移除
    clearInterval(hbTimer);
    const payload = {
      selector_hint: hint(locked),
      action: action,
      columns: columns,
      rect: rectOf(locked),
      url: location.href,  // 确认那一刻的落点（跨页框选时≠拾取起点页）
    };
    window.__pageplay_locked = locked;
    window.__pageplay_last_payload = payload;
    if (window.__pageplay_onconfirm) window.__pageplay_onconfirm(payload);
  }
  function disarm() { if (ac) ac.abort(); }
  function teardown() {
    disarm();
    if (overlay) overlay.remove();
  }
  function cancelPick() {
    over = true;
    clearInterval(hbTimer);
    teardown();
    if (window.__pageplay_oncancel) window.__pageplay_oncancel();
  }
  window.__pageplay_cancel = cancelPick;
  window.__pageplay_cleanup = () => { over = true; clearInterval(hbTimer); teardown(); };

  function onMove(e) {
    if (locked || overlay.contains(e.target)) return;
    current = semantic(e.target);
    if (current) moveBox(current);
  }
  function onClick(e) {
    if (locked) return;  // 已锁定：后续点击还给页面/面板
    if (overlay.contains(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    locked = current || semantic(e.target);
    moveBox(locked);
    if (locked.matches("table,[role=grid]")) renderColumnBar(locked);
    else renderActionPanel();
  }
  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); cancelPick(); return; }
    if (locked) return;  // 锁定后 ↑↓ 不再改目标
    if (e.key === "ArrowUp") {
      const p = current && current.parentElement;
      if (p) { upStack.push(current); current = p; moveBox(p); e.preventDefault(); }
    } else if (e.key === "ArrowDown") {
      if (upStack.length) { current = upStack.pop(); moveBox(current); e.preventDefault(); }
    }
  }
  function btn(label) {
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText = "margin:2px 4px 2px 0;padding:3px 10px;cursor:pointer;";
    return b;
  }
  function readHeaders(t) {
    let cells = t.querySelectorAll("thead th");
    if (!cells.length) cells = t.querySelectorAll("tr:first-child th");
    if (!cells.length) cells = t.querySelectorAll("tr:first-child td");
    return Array.prototype.map.call(cells, c => (c.textContent || "").trim());
  }
  function renderColumnBar(t) {
    panel.textContent = "";
    const title = document.createElement("div");
    title.textContent = "已锁定表格，选择要抓取的列：";
    panel.appendChild(title);
    const cols = readHeaders(t);
    const boxes = cols.map(text => {
      const lab = document.createElement("label");
      lab.style.cssText = "display:block;";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(text || "(未命名列)"));
      panel.appendChild(lab);
      return cb;
    });
    const ok = btn("确认");
    ok.onclick = () => confirmPick("table", cols.filter((c, i) => boxes[i].checked));
    panel.appendChild(ok);
    panel.style.display = "block";
  }
  function renderActionPanel() {
    panel.textContent = "";
    const dl = btn("下载此元素");
    dl.onclick = () => confirmPick("download", null);
    const tb = btn("抓取此表");
    tb.onclick = () => confirmPick("table", null);
    panel.appendChild(dl);
    panel.appendChild(tb);
    panel.style.display = "block";
  }
  function arm() {  // 文档级监听（AbortController 一把拆），已布防则跳过
    if (ac && !ac.signal.aborted) return;
    ac = new AbortController();
    const opt = {capture: true, signal: ac.signal};
    document.addEventListener("mousemove", onMove, opt);
    document.addEventListener("click", onClick, opt);
    document.addEventListener("keydown", onKey, opt);
  }
  function deploy() {  // 布防（幂等）：文档就绪才建 UI；UI 在且监听在则不动
    if (over || !document.body) return;
    if (!alive()) buildUi();
    arm();
  }

  // 启动：文档就绪立即布防；此后心跳自愈——覆层被页面剥掉或监听被拆
  // （软跳转/DOM 重建）时按 HB_MS 周期自动重新布防
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", deploy, {once: true});
  } else {
    deploy();
  }
  hbTimer = setInterval(deploy, HB_MS);
})();
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
    """返回注入 JS（IIFE，见 _OVERLAY_JS；行为契约见模块 docstring）。"""
    return _OVERLAY_JS


class PickCancelled(RuntimeError):
    """人取消了拾取（Esc / 窗口关闭 / 超时无人确认）。"""


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


def run_pick(page, on_confirm) -> dict:
    """注入框选覆层，等人确认，返回组装结果并回调 on_confirm。

    流程：add_init_script(overlay_js)——每个新文档加载时自动布防（跨页
    存活的关键），随后对当前已加载文档补一次 evaluate（同一份 JS 幂等；
    init script 不覆盖已加载页）→ 等待 JS 调 __pageplay_onconfirm（经
    expose 绑定收 payload，跨文档持续有效）→ page.evaluate
    (COLLECT_CHAIN_JS) 收集锁定元素到根的链 → selector_from_chain 生成
    正式 selector → 组装 {"selector","action","columns","rect","url"} →
    调 on_confirm(结果)（cli 落盘/确认打印钩子）→ 返回。

    url 语义 = 用户点确认那一刻的 location.href（跨页框选时即实际落点
    页 URL），与 cli 记账用的 page.url 同文档取值一致；payload 缺 url
    时兜底 page.url。

    Esc / 窗口关闭 / 超时无人确认 raise PickCancelled（超时自进入拾取
    起算，跨页导航不重置）。返回前覆层已清理，页面业务 DOM 不留痕。
    """
    state = {"event": threading.Event(), "cancelled": False, "payload": None}
    _ACTIVE["state"] = state
    _ensure_bindings(page)
    try:
        page.add_init_script(overlay_js())  # 新文档自动布防（跨页存活）
        page.evaluate(overlay_js())         # 当前文档补注入（幂等）
        deadline = time.monotonic() + _PICK_TIMEOUT_SEC
        while not state["event"].is_set():
            if time.monotonic() >= deadline:
                raise PickCancelled(f"拾取超时（{_PICK_TIMEOUT_SEC}s 无人确认）")
            try:
                # 等待期间 playwright 派发绑定回调（confirm/cancel 经此进入）
                page.wait_for_timeout(100)
            except Exception as exc:
                raise PickCancelled(f"页面已关闭，拾取中止：{exc}") from exc
        if state["cancelled"]:
            raise PickCancelled("人按 Esc 取消了框选")
        payload = state["payload"] or {}
        chain = page.evaluate(COLLECT_CHAIN_JS)
        selector = selector_from_chain(chain)
        result = {
            "selector": selector,
            "action": payload.get("action"),
            "columns": payload.get("columns"),
            "rect": payload.get("rect"),
            "url": payload.get("url") or page.url,
        }
        on_confirm(result)
        return result
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
