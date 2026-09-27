"""框选覆层主 JS（与 _overlay_panel_js 的面板函数配套）：picker.overlay_js()
把 PANEL_JS 拼进 /* __PANEL_JS__ */ 标记位后注入。

交互（任务书 v2 B，逐条实现）：
- 未锁定：hover 红框按候选层默认层显示（列表：N 条记录 · ↑↓ 换范围）；
  ↑/↓ 在候选层间切换（↓ 内层 / ↑ 外层）；Esc 退出框选（oncancel）。
- 已锁定：点页面别处（面板外）直接改选到新位置；Esc 或面板「重选」解锁
  回未锁定态（会话不结束）；面板「退出」才结束（oncancel）；
  Shift+点第二条按 §3.2 两点消歧（保持）。
- 页面顶部居中提示条随状态切换文案（未锁定/已锁定）。

选层（任务书 v2 A）：hover/点击都走 window.__pp_groups（全部候选层，
排除 body/html 容器）→ __pp_default（记录数最多，同数取内层）；
__pp_scan 只接收已选层。覆层里 ↑↓ 与换层条在候选层间移动。

只 append 覆层（#__pageplay_overlay），不改页面业务 DOM；经
add_init_script 注入时每个新文档自动布防，覆层被页面剥掉（SPA 软跳转）
由心跳自愈重新布防；全程幂等、单实例。
"""

OVERLAY_JS = """
(() => {
  if (window.__pageplay_cleanup) { try { window.__pageplay_cleanup(); } catch (e) {} }

  const SEM = "table,[role=grid]";
  const HB_MS = 800;  // 自愈心跳周期：覆层被剥后最迟一个周期重新布防
  let current = null, locked = null;
  let hoverGroups = null, hoverIdx = -1, lastDefContainer = null;
  let lockGroups = null, lockIdx = -1;
  let curScan = null, curCols = null, firstPickEl = null;
  let overlay = null, box = null, panel = null, lab = null, tip = null;
  let ac = null, hbTimer = 0, over = false;
  let lastScanContainer = null, lastScan = null, lastColnames = null;  // 扫描按容器+列名记忆缓存


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
  function listLabel(scan) {  // 未锁定标签：列 <2 = 垃圾组，不冒充列表
    if (!scan || !scan.columns || scan.columns.length < 2)
      return "区域：未识别到列表结构";
    return "列表：" + scan.n_records + " 条记录 · ↑↓ 换范围";
  }
  function labelFor(el) {
    if (hoverGroups && el === hoverGroups[hoverIdx].container)
      return listLabel(lastScan);
    try {
      if (el && el.matches && el.matches("table,[role=grid]"))
        return "表格：" + el.querySelectorAll("tr").length + " 行";
    } catch (e) {}
    return "元素（无表格结构，仅可下载）";
  }
  function showBox(el) {
    moveBox(el);
    const r = rectOf(el);
    lab.textContent = labelFor(el);
    lab.style.left = r.x + "px";
    lab.style.top = Math.max(0, r.y - 22) + "px";
    lab.style.display = "block";
  }
  function setHint(isLocked) {  // 顶部提示条随状态切换文案
    if (tip) tip.textContent = isLocked
      ? "勾选要的列后点确认 · 点别处换位置 · Esc 重选"
      : "点一下列表锁定 · ↑↓ 换范围 · Esc 退出";
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
  function buildUi() {  // 覆层四件套（红框/面板/标签/提示条）挂 body
    current = null; locked = null;
    hoverGroups = null; hoverIdx = -1; lastDefContainer = null;
    lockGroups = null; lockIdx = -1;
    curScan = null; curCols = null; firstPickEl = null;
    lastScanContainer = null; lastScan = null;
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
    lab = document.createElement("div");
    lab.id = "__pageplay_boxlabel";
    lab.style.cssText = "position:fixed;display:none;pointer-events:none;z-index:2147483647;"
      + "background:#fff;border:1px solid #ccc;border-radius:4px;padding:2px 8px;"
      + "font:12px/1.6 -apple-system,sans-serif;color:#222;white-space:nowrap;"
      + "box-shadow:0 2px 8px rgba(0,0,0,.12);";
    tip = document.createElement("div");
    tip.id = "__pageplay_hint";
    tip.style.cssText = "position:fixed;top:0;left:50%;transform:translateX(-50%);"
      + "z-index:2147483647;display:none;pointer-events:none;background:rgba(229,72,77,.92);"
      + "color:#fff;border-radius:0 0 6px 6px;padding:4px 14px;"
      + "font:13px/1.8 -apple-system,sans-serif;";
    overlay.appendChild(box);
    overlay.appendChild(panel);
    overlay.appendChild(lab);
    overlay.appendChild(tip);
    (document.body || document.documentElement).appendChild(overlay);
    setHint(false);
    tip.style.display = "block";
  }
  function alive() {  // 覆层五件套仍全部挂在文档里
    return !!(overlay && box && panel && lab && tip && overlay.isConnected
      && box.isConnected && panel.isConnected && lab.isConnected && tip.isConnected);
  }
  function confirmPick(payload) {  // 统一确认出口：rect/url 在此附上
    if (!locked) return;
    over = true;  // 会话结束：停自愈，UI 留给 python 收尾移除
    clearInterval(hbTimer);
    payload.rect = rectOf(locked);
    payload.url = location.href;
    window.__pageplay_locked = locked;
    window.__pageplay_last_payload = payload;
    if (window.__pageplay_onconfirm) window.__pageplay_onconfirm(payload);
  }
  function disarm() { if (ac) ac.abort(); }
  function teardown() {
    disarm();
    if (overlay) overlay.remove();
  }
  function cancelPick() {  // 结束框选会话（未锁定 Esc / 面板「退出」）
    over = true;
    clearInterval(hbTimer);
    teardown();
    if (window.__pageplay_oncancel) window.__pageplay_oncancel();
  }
  function unlock() {  // 解锁回未锁定态：面板收起，会话不结束
    locked = null; lockGroups = null; lockIdx = -1;
    curScan = null; curCols = null; firstPickEl = null;
    panel.style.display = "none";
    box.style.display = "none";
    lab.style.display = "none";
    setHint(false);
  }
  window.__pageplay_cancel = cancelPick;
  window.__pageplay_cleanup = () => { over = true; clearInterval(hbTimer); teardown(); };

  function scanFor(group) {  // 扫描按容器缓存（mousemove/↑↓ 高频）；
    // 列名记忆更换时也失效（任务书 v2 C1 规则① 注入时机）
    if (group.container !== lastScanContainer || lastColnames !== window.__pp_colnames) {
      lastScanContainer = group.container;
      lastColnames = window.__pp_colnames;
      lastScan = window.__pp_scan(group);
    }
    return lastScan;
  }
  function hoverAt(el) {  // 候选层 + 当前层（↑↓ 的 hoverIdx 悬停期间保持）
    if (!window.__pp_groups) return null;
    const gs = window.__pp_groups(el);
    if (!gs.length) { hoverGroups = null; lastDefContainer = null; return null; }
    const def = window.__pp_default(gs);
    if (!hoverGroups || def.container !== lastDefContainer) {
      hoverGroups = gs;
      hoverIdx = gs.indexOf(def);
      lastDefContainer = def.container;
    }
    const g = hoverGroups[hoverIdx] || gs[hoverIdx] || gs[0];
    return {group: g, groups: gs, idx: hoverGroups.indexOf(g), scan: scanFor(g)};
  }
  function applyList(groups, idx, scan, firstEl) {  // 锁定列表层 → 面板
    locked = groups[idx].container;
    lockGroups = groups; lockIdx = idx;
    firstPickEl = firstEl || null;
    curScan = scan || window.__pp_scan(groups[idx])
      || {columns: [], sub_sem: null, n_records: groups[idx].records.length,
          n_items: 0, sample_keys: [], record_selector: null};
    curCols = (curScan.columns || []).map(c => ({
      key: c.key, name: c.name, auto_name: c.auto_name, strip: c.strip || null,
      sem: c.sem || "", renamed: !!c.renamed, checked: c.default_checked !== false,
    }));
    setHint(true);
    renderListPanel();
  }
  function onMove(e) {
    if (locked || overlay.contains(e.target)) return;
    const semEl = semantic(e.target);
    if (semEl !== e.target) {  // ① 语义容器优先（表格路径原样）
      hoverGroups = null; current = semEl;
    } else {
      const h = hoverAt(e.target);  // ② 列表候选层（默认层；↑↓ 可换）
      current = h ? h.group.container : semEl;  // ③ 兜底：元素自身
    }
    if (current) showBox(current);
  }
  function onClick(e) {
    if (overlay.contains(e.target)) return;  // 面板内点击还给面板
    if (locked && e.shiftKey && firstPickEl) {  // §3.2 两点消歧（保持）
      const g2 = window.__pp_group2(firstPickEl, e.target);
      if (g2 && g2.records.length >= 2) applyList([g2], 0, window.__pp_scan(g2), e.target);
      return;
    }
    firstPickEl = e.target;  // 已锁定再点 = 直接换选（不经取消）
    const semEl = semantic(e.target);
    if (semEl !== e.target) {  // 语义表格（原样）
      locked = semEl;
      lastScanContainer = null; lastScan = null;
      renderColumnBar(locked);
    } else {
      const h = hoverAt(e.target);
      if (h) applyList(h.groups, h.idx, h.scan, e.target);
      else { locked = semEl || e.target; renderActionPanel(); }
    }
    setHint(true);
    showBox(locked);
  }
  function onKey(e) {
    if (e.key === "Escape") {
      const ae = document.activeElement;
      if (ae && ae.tagName === "INPUT") return;  // 改名输入框里 Esc 只退输入
      e.preventDefault();
      if (locked) unlock();  // 已锁定：解锁重选，会话不结束
      else cancelPick();     // 未锁定：退出框选
      return;
    }
    if (locked) return;  // 锁定后换层走面板/换选，↑↓ 不再改目标
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {  // 候选层切换
      if (!hoverGroups || !hoverGroups.length) return;
      const next = e.key === "ArrowUp" ? hoverIdx + 1 : hoverIdx - 1;
      if (next < 0 || next >= hoverGroups.length) return;  // 到边不动
      hoverIdx = next;
      const g = hoverGroups[hoverIdx];
      current = g.container;
      scanFor(g);  // 换层即重扫（缓存按容器，标签/列用新层）
      showBox(current);  // 红框和标签实时更新
      e.preventDefault();
      return;
    }
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

/* __PANEL_JS__ */
})();
"""
