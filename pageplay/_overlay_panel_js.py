"""框选面板 JS 资产（与 _overlay_js 的主 IIFE 配套）：面板渲染函数集。

本模块不是独立 IIFE——picker.overlay_js() 把 PANEL_JS 拼进主 IIFE 内部
的 /* __PANEL_JS__ */ 标记位，函数与主文件共享闭包状态（locked /
curScan / curCols / lockGroups / lockIdx / unlock / cancelPick）。

面板布局（任务书 v2 B，自上而下）：
  1. [重选 (Esc)] [退出]        右侧小字「识别到 N 条记录 · M 件商品」
  2. 换层条「◀ 小一层 · N 条 · 大一层 ▶」（候选层只有一层时不显示）
  3. 列勾选区：☑ 列名（双击改名）｜ 样例值（规则⑤列名附灰色 sem）
  4. [预览前 3 行] [确认] [只抓链接]
「只抓链接」面板：勾「同时下载文件」→ 确认出 action=links 载荷。
语义表格与普通元素路径的面板（renderColumnBar / renderActionPanel）原样。
"""

PANEL_JS = """
  // ── 公共小件 ──
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
  function checkList(labels) {  // 勾选条公共件：默认全勾的 checkbox 列表
    return labels.map(text => {
      const lab = document.createElement("label"), cb = document.createElement("input");
      lab.style.cssText = "display:block;";
      cb.type = "checkbox"; cb.checked = true;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(text || "(未命名列)"));
      panel.appendChild(lab);
      return cb;
    });
  }

  // ── 列表面板（v0.10 主路径）──
  function renderListPanel() {
    panel.textContent = "";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:4px;"
      + "justify-content:space-between;white-space:nowrap;";
    const left = document.createElement("span");
    const re = btn("重选 (Esc)");
    re.onclick = unlock;
    left.appendChild(re);
    const ex = btn("退出");
    ex.onclick = cancelPick;
    left.appendChild(ex);
    head.appendChild(left);
    const cnt = document.createElement("span");
    cnt.textContent = "识别到 " + curScan.n_records + " 条记录"
      + (curScan.sub_sem ? " · " + curScan.n_items + " 件商品" : "");
    cnt.style.cssText = "color:#666;font-size:12px;white-space:nowrap;";
    head.appendChild(cnt);
    panel.appendChild(head);
    if (lockGroups && lockGroups.length > 1) {  // 换层条（单层不显示）
      const bar = document.createElement("div");
      bar.style.cssText = "margin:3px 0;white-space:nowrap;";
      const sm = btn("◀ 小一层");
      sm.onclick = () => switchLayer(lockIdx - 1);
      bar.appendChild(sm);
      const mid = document.createElement("span");
      mid.textContent = " " + curScan.n_records + " 条 ";
      mid.style.cssText = "color:#666;font-size:12px;";
      bar.appendChild(mid);
      const bg = btn("大一层 ▶");
      bg.onclick = () => switchLayer(lockIdx + 1);
      bar.appendChild(bg);
      panel.appendChild(bar);
    }
    if (!curCols || curCols.length < 2) {  // 垃圾组：无可确认的列路径
      const note = document.createElement("div");
      note.textContent = "未识别到有效的重复数据结构";
      note.style.cssText = "color:#888;font-size:12px;";
      panel.appendChild(note);
      const dl = btn("下载此元素");
      dl.onclick = () => confirmPick({selector_hint: hint(locked), action: "download"});
      panel.appendChild(dl);
      panel.style.display = "block";
      return;
    }
    for (const c of curCols) panel.appendChild(colRow(c));
    const bar = document.createElement("div");
    const prev = btn("预览前 3 行");
    prev.onclick = togglePreview;
    bar.appendChild(prev);
    const ok = btn("确认");
    ok.onclick = confirmList;
    bar.appendChild(ok);
    const lk = btn("只抓链接");
    lk.onclick = renderLinksPanel;
    bar.appendChild(lk);
    panel.appendChild(bar);
    const cbs = panel.querySelectorAll("input[type=checkbox]");
    curCols.forEach((c, i) => {
      cbs[i].onchange = () => { c.checked = cbs[i].checked; ok.disabled = !curCols.some(x => x.checked); };
    });
    const pv = document.createElement("div");
    pv.id = "__pp_preview";
    pv.style.display = "none";
    panel.appendChild(pv);
    ok.disabled = !curCols.some(c => c.checked);
    panel.style.display = "block";
  }
  function colRow(c) {  // ☑ 列名（双击改名）｜ 样例值；规则⑤列名附灰色 sem
    const row = document.createElement("label");
    row.style.cssText = "display:block;white-space:nowrap;";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = c.checked;
    row.appendChild(cb);
    row.appendChild(document.createTextNode(" "));
    const name = document.createElement("span");
    name.textContent = c.name;
    name.title = "双击改名";
    name.style.cssText = "cursor:text;border-bottom:1px dashed #bbb;";
    name.ondblclick = () => startRename(name, c);
    row.appendChild(name);
    if (c.auto_name && /^列\\d/.test(c.auto_name) && c.sem) {
      const g = document.createElement("span");
      g.textContent = " " + c.sem;
      g.style.cssText = "color:#aaa;font-size:11px;";
      row.appendChild(g);
    }
    row.appendChild(document.createTextNode(" ｜ " + (c.sample || "")));
    if (c.renamed) {
      const m = document.createElement("span");
      m.textContent = " 已记住";
      m.style.cssText = "color:#2a7;font-size:11px;";
      row.appendChild(m);
    }
    return row;
  }
  function startRename(span, c) {  // 双击列名 → 原地输入框，Enter/失焦提交
    const input = document.createElement("input");
    input.type = "text";
    input.value = c.name;
    input.style.cssText = "width:110px;font:inherit;";
    span.replaceWith(input);
    input.focus(); input.select();
    const commit = () => {
      const v = input.value.trim();
      if (v && v !== c.name) { c.name = v; c.renamed = true; }
      input.replaceWith(span);
      span.textContent = c.name;
    };
    input.onblur = commit;
    input.onkeydown = (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
      if (ev.key === "Escape") { ev.stopPropagation(); input.value = c.name; input.blur(); }
    };
  }
  function confirmList() {  // §6：table + list_mode=list 契约载荷
    confirmPick({
      selector_hint: hint(locked),
      action: "table",
      list_mode: "list",
      record_selector: curScan.record_selector || hint(locked),
      sub_sem: curScan.sub_sem || null,
      columns: curCols.filter(c => c.checked)
        .map(c => ({key: c.key, name: c.name, auto_name: c.auto_name,
                    strip: c.strip || null})),
    });
  }
  function togglePreview() {  // 前 3 行（按当前勾选列与改名渲染）
    const pv = document.getElementById("__pp_preview");
    if (!pv) return;
    if (pv.style.display !== "none") { pv.style.display = "none"; return; }
    pv.textContent = "";
    const cols = curCols.filter(c => c.checked);
    const t = document.createElement("table");
    t.style.cssText = "border-collapse:collapse;font:12px/1.5 -apple-system,sans-serif;";
    const head = t.insertRow();
    for (const c of cols) {
      const th = document.createElement("th");
      th.textContent = c.name;
      th.style.cssText = "border:1px solid #ddd;padding:2px 6px;text-align:left;background:#f7f7f7;";
      head.appendChild(th);
    }
    for (const r of (curScan.sample_keys || [])) {
      const tr = t.insertRow();
      for (const c of cols) {
        const td = tr.insertCell();
        td.textContent = String(r[c.key] === undefined ? "" : r[c.key]);
        td.style.cssText = "border:1px solid #eee;padding:2px 6px;max-width:220px;"
          + "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      }
    }
    pv.appendChild(t);
    pv.style.display = "block";
  }
  function renderLinksPanel() {  // §4「只抓链接」：框内全部 a[href] + 可选下载
    panel.textContent = "";
    const n = locked.querySelectorAll("a[href]").length;
    const title = document.createElement("div");
    title.textContent = "抓取框内全部链接（去重后 " + n + " 条）进 CSV；";
    panel.appendChild(title);
    const dlRow = document.createElement("label");
    dlRow.style.cssText = "display:block;";
    const dlCb = document.createElement("input");
    dlCb.type = "checkbox";
    dlRow.appendChild(dlCb);
    dlRow.appendChild(document.createTextNode(" 同时下载文件（像文件的链接）"));
    panel.appendChild(dlRow);
    const ok = btn("确认");
    ok.onclick = () => confirmPick({
      selector_hint: hint(locked),
      action: "links",
      record_selector: curScan.record_selector || hint(locked),
      download: dlCb.checked,
    });
    panel.appendChild(ok);
    const back = btn("返回列选择");
    back.onclick = renderListPanel;
    panel.appendChild(back);
    panel.style.display = "block";
  }
  function switchLayer(idx) {  // 面板换层条：换层并重扫，列刷新
    if (!lockGroups || idx < 0 || idx >= lockGroups.length) return;
    lockIdx = idx;
    locked = lockGroups[idx].container;
    curScan = window.__pp_scan(lockGroups[idx])
      || {columns: [], sub_sem: null, n_records: lockGroups[idx].records.length,
          n_items: 0, sample_keys: [], record_selector: null};
    curCols = (curScan.columns || []).map(c => ({
      key: c.key, name: c.name, auto_name: c.auto_name, strip: c.strip || null,
      sem: c.sem || "", checked: c.default_checked !== false,
    }));
    showBox(locked);
    renderListPanel();
  }

  // ── 语义表格 / 普通元素（原样保留）──
  function renderColumnBar(t) {  // 语义表格锁定：读表头出列勾选
    panel.textContent = "";
    const title = document.createElement("div");
    title.textContent = "已锁定表格，选择要抓取的列：";
    panel.appendChild(title);
    const cols = readHeaders(t);
    const ok = btn("确认");
    if (!cols.length) {  // 防呆：无表格行 → 读不到列，确认置灰拦住
      const warn = document.createElement("div");
      warn.textContent = "该目标读不到列（无表格行），按 Esc 换目标";
      warn.style.cssText = "color:#e5484d;";
      panel.appendChild(warn);
      ok.disabled = true;
      ok.style.opacity = "0.5";
    } else {
      const boxes = checkList(cols);
      ok.onclick = () => confirmPick({selector_hint: hint(locked), action: "table",
                                      columns: cols.filter((c, i) => boxes[i].checked)});
    }
    panel.appendChild(ok);
    panel.style.display = "block";
  }
  function renderActionPanel() {  // 普通元素（无列表组）：下载/抓表
    panel.textContent = "";
    const dl = btn("下载此元素");
    dl.onclick = () => confirmPick({selector_hint: hint(locked), action: "download"});
    panel.appendChild(dl);
    if (locked.querySelectorAll("tr").length) {  // 有表格行才提供抓表
      const tb = btn("抓取此表");
      tb.onclick = () => confirmPick({selector_hint: hint(locked), action: "table"});
      panel.appendChild(tb);
    } else {  // 防呆：无 tr 抓表必 0 行，明说不可抓，不给假入口
      const note = document.createElement("div");
      note.textContent = "该元素无表格结构，不可抓表";
      note.style.cssText = "color:#888;font-size:12px;";
      panel.appendChild(note);
    }
    panel.style.display = "block";
  }
"""
