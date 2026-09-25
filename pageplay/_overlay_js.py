"""框选覆层注入 JS 资产（纯字符串模块，零 import）：picker.overlay_js() 返回它。

注入 JS（IIFE）：hover 高亮 + 键盘扩收 + Esc 取消 + 点击锁定/列勾选。
只 append 覆层（#__pageplay_overlay），不改页面业务 DOM；结束（确认/
取消/清理）由 __pageplay_cleanup 移除全部覆层与监听。经 add_init_script
注入时每个新文档自动布防；同文档内覆层被页面剥掉（SPA 软跳转/DOM 重建）
由心跳自愈重新布防，全程幂等、单实例。

独立成模块只为控制 picker.py 行数（v0.7 收口，红线 #26）：JS 是整块资产，
拆走后 picker.py 只剩 python 逻辑。改 JS 必须同步对照 picker 里
COLLECT_CHAIN_JS / selector_from_chain 的同规则实现（链序两端一致）。
"""

OVERLAY_JS = """
(() => {
  if (window.__pageplay_cleanup) { try { window.__pageplay_cleanup(); } catch (e) {} }

  const SEM = "table,ul,ol,[role=grid]";
  const HB_MS = 800;  // 自愈心跳周期：覆层被剥后最迟一个周期重新布防
  let current = null, curGroup = null;  // 高亮目标 / 其卡片组（null=非卡片模式）
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
  function sameGroup(el) {  // 同 tag+首class 且含非空文本的兄弟组，≥3 才算卡片签名
    if (!el || el.nodeType !== 1 || !el.parentElement) return null;
    const c = el.classList[0];
    const g = Array.prototype.filter.call(el.parentElement.children, s =>
      s.tagName === el.tagName && s.classList[0] === c && (s.textContent || "").trim());
    return g.length >= 3 ? g : null;
  }
  function cardGroup(el) {  // 自 el 向上取最大重复组 → {items, box:组父容器}；无则 null
    let best = null;
    for (let p = el; p && p.nodeType === 1; p = p.parentElement) {
      const g = sameGroup(p);
      if (g && (!best || g.length > best.items.length))
        best = {items: g, box: p.parentElement};
    }
    return best;
  }
  function relFrom(item, node) {  // item→文本宿主相对链 [{tag,nth}]（item 端在首）
    const c = [];
    for (let n = node.parentElement; n && n !== item; n = n.parentElement)
      c.unshift({tag: n.tagName.toLowerCase(), nth: tagNth(n)});
    return c;
  }
  function pickFields(item) {  // 首条记录内文本节点 → 字段候选：≥2 字、相邻同文/同宿主去重
    const out = [], w = document.createTreeWalker(item, NodeFilter.SHOW_TEXT);
    let last = null, m;
    while ((m = w.nextNode())) {
      const t = (m.textContent || "").trim();
      if (t.length < 2 || (last && (t === last.t || m.parentElement === last.el))) continue;
      last = {t: t, el: m.parentElement};
      out.push({label: t.slice(0, 8) || "字段" + (out.length + 1), rel: relFrom(item, m)});
    }
    return out;
  }
  function buildUi() {  // 建覆层三件套并挂到 body（重建时同步重置拾取态）
    current = null; locked = null; curGroup = null; upStack.length = 0;
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
  function confirmPick(action, columns, listMode, fields) {
    if (!locked) return;
    over = true;  // 会话结束：停自愈，UI 留给 python 收尾移除
    clearInterval(hbTimer);
    const payload = {
      selector_hint: hint(locked),
      action: action,
      columns: columns,
      list_mode: listMode || "table",  // 卡片组确认传 "cards"，其余恒 "table"
      fields: fields || null,          // 仅卡片组带 [{label,rel}]，其余 null
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
    const sem = semantic(e.target);
    curGroup = null;
    if (sem !== e.target) current = sem;  // ① 语义容器优先（表格路径原样）
    else {
      const g = cardGroup(e.target);      // ② 次选：重复兄弟组，高亮其父容器
      if (g) { curGroup = g; current = g.box; }
      else current = sem;                 // ③ 兜底：元素自身
    }
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
    else if (curGroup && locked === curGroup.box) renderFieldBar(curGroup);
    else renderActionPanel();
  }
  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); cancelPick(); return; }
    if (locked) return;  // 锁定后 ↑↓ 不再改目标
    if (e.key === "ArrowUp") {
      const p = current && current.parentElement;
      if (p) { upStack.push(current); current = p; curGroup = null;
               moveBox(p); e.preventDefault(); }
    } else if (e.key === "ArrowDown") {
      if (upStack.length) { current = upStack.pop(); curGroup = null;
                            moveBox(current); e.preventDefault(); }
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
  function checkList(labels) {  // 勾选条公共件：label 列表 → 默认全勾的 checkbox 列表
    return labels.map(text => {
      const lab = document.createElement("label"), cb = document.createElement("input");
      lab.style.cssText = "display:block;";
      cb.type = "checkbox"; cb.checked = true;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(text || "(未命名列)"));
      panel.appendChild(lab);
      return cb;
    });
  }
  function renderColumnBar(t) {
    panel.textContent = "";
    const title = document.createElement("div");
    title.textContent = "已锁定表格，选择要抓取的列：";
    panel.appendChild(title);
    const cols = readHeaders(t);
    const boxes = checkList(cols);
    const ok = btn("确认");
    ok.onclick = () => confirmPick("table", cols.filter((c, i) => boxes[i].checked));
    panel.appendChild(ok);
    panel.style.display = "block";
  }
  function renderFieldBar(g) {  // 卡片组锁定：识别到 N 条记录 + 字段勾选（确认=抓卡片）
    panel.textContent = "";
    const title = document.createElement("div");
    title.textContent = "识别到 " + g.items.length + " 条记录，选择要抓取的字段：";
    panel.appendChild(title);
    const fields = pickFields(g.items[0]);
    const boxes = checkList(fields.map(f => f.label));
    const ok = btn("确认");
    ok.onclick = () => confirmPick("table", null, "cards",
      fields.filter((f, i) => boxes[i].checked));
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
