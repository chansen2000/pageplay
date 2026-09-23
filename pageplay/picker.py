"""框选层：人在页面上圈表格/元素，产出正式 CSS 选择器与动作载荷。

流程（T7c 任务书契约）：run_pick 注入 overlay_js（hover 高亮 + ↑ 扩选 /
↓ 收回 + Esc 取消 + 点击锁定）→ 人确认后 JS 调 window.__pageplay_onconfirm
→ python 经 page.expose_function 收 payload → 用 COLLECT_CHAIN_JS 收集
锁定元素到根的链 → selector_from_chain 生成正式 selector → 组装返回。
全程不改页面业务 DOM（只 append 覆层/面板，结束移除）。

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
_OVERLAY_JS = """
(() => {
  if (window.__pageplay_cleanup) { try { window.__pageplay_cleanup(); } catch (e) {} }

  const SEM = "table,ul,ol,[role=grid]";
  let current = null;   // 当前高亮目标
  let locked = null;    // 点击锁定后的目标
  const upStack = [];   // ↑ 扩选记录（↓ 收回用）

  const overlay = document.createElement("div");
  overlay.id = "__pageplay_overlay";
  const box = document.createElement("div");
  box.style.cssText = "position:fixed;display:none;pointer-events:none;z-index:2147483646;"
    + "border:2px solid #e5484d;background:rgba(229,72,77,.10);box-sizing:border-box;";
  const panel = document.createElement("div");
  panel.style.cssText = "position:fixed;top:12px;right:12px;display:none;z-index:2147483647;"
    + "background:#fff;border:1px solid #ccc;border-radius:6px;padding:10px 12px;"
    + "font:13px/1.6 -apple-system,sans-serif;color:#222;"
    + "box-shadow:0 4px 16px rgba(0,0,0,.15);max-height:70vh;overflow:auto;";
  overlay.appendChild(box);
  overlay.appendChild(panel);
  (document.body || document.documentElement).appendChild(overlay);

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
  function confirmPick(action, columns) {
    if (!locked) return;
    const payload = {
      selector_hint: hint(locked),
      action: action,
      columns: columns,
      rect: rectOf(locked),
    };
    window.__pageplay_locked = locked;
    window.__pageplay_last_payload = payload;
    if (window.__pageplay_onconfirm) window.__pageplay_onconfirm(payload);
  }
  function teardown() {
    document.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("click", onClick, true);
    document.removeEventListener("keydown", onKey, true);
    overlay.remove();
  }
  function cancelPick() {
    teardown();
    if (window.__pageplay_oncancel) window.__pageplay_oncancel();
  }
  window.__pageplay_cancel = cancelPick;
  window.__pageplay_cleanup = teardown;

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
  document.addEventListener("mousemove", onMove, true);
  document.addEventListener("click", onClick, true);
  document.addEventListener("keydown", onKey, true);
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
    """向页面暴露确认/取消绑定（同页重复拾取时绑定已存在，跳过）。"""

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

    if page.evaluate("() => typeof window.__pageplay_onconfirm") == "function":
        return
    page.expose_function("__pageplay_onconfirm", _on_confirm)
    page.expose_function("__pageplay_oncancel", _on_cancel)


def run_pick(page, on_confirm) -> dict:
    """注入框选覆层，等人确认，返回组装结果并回调 on_confirm。

    流程：注入 overlay_js → 等待 JS 调 __pageplay_onconfirm（经 expose
    绑定收 payload）→ page.evaluate(COLLECT_CHAIN_JS) 收集锁定元素到根
    的链 → selector_from_chain 生成正式 selector → 组装
    {"selector","action","columns","rect"} → 调 on_confirm(结果)（cli
    落盘/确认打印钩子）→ 返回。

    Esc / 窗口关闭 / 超时无人确认 raise PickCancelled。返回前覆层已清理，
    页面业务 DOM 不留痕。
    """
    state = {"event": threading.Event(), "cancelled": False, "payload": None}
    _ACTIVE["state"] = state
    _ensure_bindings(page)
    try:
        page.evaluate(overlay_js())
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
