"""列表扫描 JS 资产（纯字符串模块，零 import）：listscan.py / 覆层注入并调用。

对外入口（挂 window，框选面板预览与 run 重放共用同一份算法，设计 §3、
任务书 v2 A/C）：
  __pp_groups(el)      → el 向上全部候选层 [{container,records,recordSem}]
                         （内→外；排除 body/html 容器）——选层的唯一来源
  __pp_default(groups) → 默认层：记录数最多，同数取内层
  __pp_scan(group)     → 只接收已选好的层，产出跨记录列统计（不再自己找层）
  __pp_group2(a, b)    → §3.2 两点消歧（Shift+点第二条），返回单项与候选层同构
  __pp_extract(spec)   → 按 spec 抽全部行（与 scan 同一条 fieldsOf 管线）

模型：记录(record) → 子项(sub-item) → 字段(sem 路径键)，全部跨记录统计：
- sem(el)：首个 class 去掉 "--" 及后缀；无 class 取 tag 小写（§3.1）。
- 子项：某记录内同父兄弟 ≥2 次的 sem，且出现在 ≥50% 记录里；只认一层
  （§3.3）。多候选按 (带语义 class 占比, 同父完整度, 覆盖记录数) 排序。
- 字段：可见文本叶子（跳 SCRIPT/STYLE/NOSCRIPT、隐藏宿主），几何用文本
  Range rect；同行 = 与组垂直重叠 ≥ 较小高度 50%（字体基线差不敏感）
  且水平相接（右邻 ≤3px 或重叠 ≤3px，跨列交错不并），且 label 系不并入
  非 label 组（真机"实付款"标签与价格碎片相贴）；行内文本按 left 序拼接；
  键 = 相对 root 的 sem 路径 + 每级同 sem 兄弟位次（k=1 省略，§6 键形）；
  宿主在 a[href] 内 → 额外键 <key>_链接（绝对 URL）。
- 列名五级优先（任务书 v2 C1，命中即停）：①本站记住的名字
  （window.__pp_colnames[key]，picker 从 colnames.json 注入）②紧挨标签
  （同容器内紧挨在前、sem 含 label 或文本 ≤8 字以冒号结尾 → 标签文字
  作列名，标签列删除）③值前缀 prefixName ④词典翻译（sem 拆词去填充词
  查 DICT；末词属时间/序号类取「前一词+末词」）⑤兜底 列N。
- 默认勾选（C2）：按钮类不勾（宿主在 button/[role=button] 内或 sem 含
  button|btn|operate|operation|action，链接列随主列）→ 全行同值不勾 →
  其余勾。
- 列：键出现在 ≥50% 行才保留；撞名列加 #2/#3；链接列名 = 主列名+「链接」。
- 选择器：带 "--" 的 class 用 [class*="语义名"]，不写哈希后缀（§3.1/§6）。
"""

LISTSCAN_JS = """
(() => {
  const SKIP_TAGS = /^(SCRIPT|STYLE|NOSCRIPT)$/;
  const OVERLAP_RATIO = 0.5;  // 同行判定：与组垂直重叠 ≥ 较小高度的 50%
  const GAP_TOL = 3;          // 碎片合并：水平间距 ≤3px（字段间 ≥8px）
  const NAME_MAX = 20;        // 列名前缀（"xx:"）最大长度，防长文本误判

  // C1 规则 4：填充词与词典（写死常量，只加不删）
  const FILLER = /^(info|container|wrap|wrapper|box|inner|content|col|main|sub|left|right|trade|clamp|symbol|text|inline)$/;
  const DICT = {order: "订单", time: "时间", date: "日期", id: "号", no: "号",
    shop: "店铺", store: "店铺", seller: "卖家", name: "名称", status: "状态",
    title: "标题", price: "价格", amount: "金额", total: "合计", pay: "支付",
    payment: "实付", quantity: "数量", qty: "数量", count: "数量", num: "数量",
    sku: "规格", spec: "规格", item: "商品", goods: "商品", product: "商品",
    express: "快递", logistics: "物流", address: "地址", phone: "电话",
    user: "用户", buyer: "买家", remark: "备注", desc: "描述", image: "图片",
    img: "图片", link: "链接", button: "按钮", btn: "按钮"};
  const DICT_PAIR = /^(time|date|id|no|name|status|count|num)$/;
  const BTN_SEM = /button|btn|operate|operation|action/i;
  const SEM_IDX_RE = /#\\d+$/;

  // ==== 基础：sem / 规整 / 选择器 ====
  function sem(el) {
    if (!el || el.nodeType !== 1) return "";
    const c = el.classList && el.classList[0];
    if (!c) return el.tagName.toLowerCase();
    const i = c.indexOf("--");
    return i > 0 ? c.slice(0, i) : c;
  }
  function norm(s) { return (s || "").replace(/\\s+/g, " ").trim(); }
  function selLink(el) {  // 单级选择器：哈希 class 用 [class*="语义名"]，不写哈希
    const tag = el.tagName.toLowerCase();
    const c = el.classList && el.classList[0];
    if (!c) return tag;
    const i = c.indexOf("--");
    if (i > 0) return tag + '[class*="' + c.slice(0, i) + '"]';
    return tag + "." + c;
  }
  function containerSelector(container) {  // 容器 sem 链 → 选择器（≤3 级）
    const links = [];
    for (let n = container; n && n.nodeType === 1 && n !== document.body;
         n = n.parentElement) links.unshift(selLink(n));
    return links.slice(-3).join(" > ");
  }
  function recordSelector(container, recordEl) {  // §6 record_selector
    return containerSelector(container) + " > " + selLink(recordEl);
  }

  // ==== 选层（任务书 v2 A）====
  function sameSemChildren(p, s) {
    return Array.prototype.filter.call(p.children, c => sem(c) === s);
  }
  function groupsOf(el) {  // el 向上全部候选层（内→外），排除 body/html 容器
    const out = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const p = n.parentElement;
      if (!p) break;
      if (p.tagName.toLowerCase() === "body" || p.tagName.toLowerCase() === "html")
        break;
      const sibs = sameSemChildren(p, sem(n));
      if (sibs.length >= 3)
        out.push({container: p, records: sibs, recordSem: sem(n)});
    }
    return out;
  }
  function defaultGroup(groups) {  // 记录数最多；同数取内层（数组靠前）
    let best = null;
    for (const g of groups)
      if (!best || g.records.length > best.records.length) best = g;
    return best;
  }
  function scanGroup2(a, b) {  // §3.2 点 2 条：LCA 子层取含两点的同 sem 兄弟
    const anc = [];
    for (let n = a; n; n = n.parentElement) anc.push(n);
    let lca = null;
    for (let n = b; n && !lca; n = n.parentElement)
      for (const x of anc) if (x === n) { lca = n; break; }
    if (!lca) return null;
    let ca = null, cb = null;
    for (const c of lca.children) {
      if (ca === null && c.contains(a)) ca = c;
      if (cb === null && c.contains(b)) cb = c;
    }
    if (!ca || !cb || ca === cb) return null;
    const s = sem(ca);
    if (s !== sem(cb)) return null;
    return {container: lca, records: sameSemChildren(lca, s), recordSem: s};
  }
  function itemRootsOf(rec, subSem) {  // 记录内 subSem 同父兄弟最多的一组
    let best = [];
    for (const el of rec.querySelectorAll("*")) {
      if (sem(el) !== subSem) continue;
      const sibs = sameSemChildren(el.parentElement, subSem);
      if (sibs.length > best.length) best = sibs;
    }
    return best;
  }
  function findSubSem(records) {  // §3.3：同父兄弟 ≥2 次的 sem，覆盖 ≥50% 记录
    const cands = {};  // sem → {recs, tot, grp, mult, classed}
    for (const rec of records) {
      const bySem = new Map();  // sem → Map(parent → 计数)
      for (const el of rec.querySelectorAll("*")) {
        const s = sem(el);
        if (!s || s === sem(rec)) continue;
        let m = bySem.get(s);
        if (!m) bySem.set(s, m = new Map());
        m.set(el.parentElement, (m.get(el.parentElement) || 0) + 1);
      }
      for (const [s, m] of bySem) {
        const d = cands[s]
          || (cands[s] = {recs: 0, tot: 0, grp: 0, mult: 0, classed: 0});
        d.recs++;
        let best = 0;
        for (const c of m.values()) { d.tot += c; if (c > best) best = c; }
        d.grp += best;
        if (best >= 2) d.mult++;
        const any = m.keys().next().value;
        if (any.classList && any.classList[0]) d.classed++;
      }
    }
    let best = null, bscore = null;
    for (const s in cands) {
      const d = cands[s];
      if (d.mult < 1 || d.recs * 2 < records.length) continue;
      const score = [d.classed / d.recs, d.grp / d.tot, d.recs, d.mult];
      if (!bscore || better(score, bscore)) { bscore = score; best = s; }
    }
    return best;
  }
  function better(a, b) {  // 字典序比较（带容差）
    for (let i = 0; i < a.length; i++) {
      if (a[i] > b[i] + 1e-9) return true;
      if (a[i] < b[i] - 1e-9) return false;
    }
    return false;
  }

  // ==== 字段采集 ====
  function semIndex(n) {  // 同 sem 兄弟中的位次（1 起）
    const s = sem(n);
    let k = 1;
    for (let p = n.previousElementSibling; p; p = p.previousElementSibling)
      if (sem(p) === s) k++;
    return k;
  }
  function pathOf(root, el) {  // root 排除自身的 sem 路径，每级带同 sem 位次
    const parts = [];
    for (let n = el; n && n !== root; n = n.parentElement) parts.unshift(n);
    if (!parts.length) return null;  // el 即 root：无相对路径
    const out = [];
    for (const n of parts) {
      const s = sem(n), k = semIndex(n);
      out.push(s + (k > 1 ? "#" + k : ""));
    }
    return out.join(">");
  }
  function keyElementOf(root, host, value) {  // textContent 恰等于值的最深元素
    let kel = host;
    while (kel && kel !== root && norm(kel.textContent) !== value)
      kel = kel.parentElement;
    if (!kel || kel === root || norm(kel.textContent) !== value) return host;
    return kel;
  }
  function fieldsOf(root, exclude) {  // root 内可见文本叶子 → 合并碎片 → 字段
    const out = [];
    if (!root) return out;
    const excl = exclude || [];
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const leaves = [], hostMap = new Map();
    let t;
    while ((t = w.nextNode())) {
      const host = t.parentElement;
      if (!host || SKIP_TAGS.test(host.tagName)) continue;
      let skip = false;
      for (const x of excl) if (x && x.contains(host)) { skip = true; break; }
      if (skip) continue;
      const txt = t.textContent || "";
      if (!txt.trim()) continue;  // 纯空白不进候选
      const hr = host.getBoundingClientRect();
      if (hr.width === 0 && hr.height === 0) continue;  // 隐藏文本（旺旺在线）
      const range = document.createRange();
      range.selectNodeContents(t);
      const tr = range.getClientRects()[0];  // 文本首行 rect：字形位置
      if (!tr || tr.width === 0) continue;
      if (hostMap.has(host)) { hostMap.get(host).text += txt; continue; }
      const anchor = host.closest("a[href]");
      const info = {host: host, text: txt, rect: tr,
                    isLabel: /label/i.test(sem(host)),
                    link: (anchor && root.contains(anchor)) ? anchor.href : null};
      hostMap.set(host, info);
      leaves.push(info);
    }
    // 同行（垂直重叠过半）且水平相接的相邻碎片归并为一组；label 系不跨组；
    // 行内文本按 left 序拼接（阅读序）
    const groups = [];
    for (const lf of leaves) {
      const g = groups[groups.length - 1];
      let join = false;
      if (g) {
        const ov = Math.min(lf.rect.bottom, g.bottom)
                 - Math.max(lf.rect.top, g.top);
        const smaller = Math.min(lf.rect.bottom - lf.rect.top,
                                 g.bottom - g.top);
        join = ov >= OVERLAP_RATIO * Math.max(smaller, 0.1)
          && lf.isLabel === g.isLabel
          && lf.rect.left <= g.right + GAP_TOL
          && lf.rect.right >= g.left - GAP_TOL;
      }
      if (join) {
        g.members.push(lf);
        if (lf.rect.right > g.right) g.right = lf.rect.right;
        if (lf.rect.left < g.left) g.left = lf.rect.left;
        if (lf.rect.top < g.top) g.top = lf.rect.top;
        if (lf.rect.bottom > g.bottom) g.bottom = lf.rect.bottom;
        if (lf.link && !g.link) g.link = lf.link;
      } else {
        groups.push({members: [lf], top: lf.rect.top, bottom: lf.rect.bottom,
                     left: lf.rect.left, right: lf.rect.right,
                     isLabel: lf.isLabel, link: lf.link});
      }
    }
    for (const g of groups) {
      g.members.sort((a, b) => a.rect.left - b.rect.left);
      const value = norm(g.members.map(m => m.text).join(""));
      const host = g.members[0].host;
      const kel = value ? keyElementOf(root, host, value) : null;
      out.push({value: value,
                key: value ? pathOf(root, kel) : null,
                parentEl: kel ? kel.parentElement : null,
                isLabel: g.isLabel
                  || (value.length <= 8 && /[:：]$/.test(value)),
                btn: /button|btn|operate|operation|action/i.test(sem(host))
                  || !!(host.closest && host.closest("button,[role=button]")),
                link: g.link});
    }
    return out.filter(m => m.value && m.key);
  }
  function prefixName(values) {  // C1 规则 3：多数值共同前缀以 :/：结尾
    const vals = values.filter(v => v);
    if (vals.length < 2) return null;
    const cand = {};
    for (const v of vals) {
      let i = v.indexOf(":");
      const j = v.indexOf("：");
      if (i < 0 || (j >= 0 && j < i)) i = j;
      if (i > 0 && i + 1 <= NAME_MAX && v.charAt(i + 1) !== "/") {
        const p = v.slice(0, i + 1);
        cand[p] = (cand[p] || 0) + 1;
      }
    }
    let best = null, bestN = 0;
    for (const p in cand) if (cand[p] > bestN) { best = p; bestN = cand[p]; }
    if (!best || bestN * 2 < vals.length) return null;
    if (!vals.some(v => v.length > best.length)) return null;
    return {strip: best, name: best.slice(0, -1)};
  }
  function dictName(semName) {  // C1 规则 4：sem 拆词去填充词查词典
    const words = semName
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .split(/[-_ ]+/).map(w => w.toLowerCase())
      .filter(w => w && !FILLER.test(w));
    if (!words.length) return null;
    const last = words[words.length - 1];
    if (!(last in DICT)) return null;
    if (words.length >= 2 && DICT_PAIR.test(last)) {
      const prev = words[words.length - 2];
      if (prev in DICT) return DICT[prev] + DICT[last];
    }
    return DICT[last];
  }
  function sameAll(vals) {
    for (let i = 1; i < vals.length; i++) if (vals[i] !== vals[0]) return false;
    return true;
  }
  function rowPut(map, f) {  // 链接字段额外产 <key>_链接 合成键（列序相邻）
    if (!f.key) return;
    map[f.key] = f;
    if (f.link)
      map[f.key + "_链接"] = {value: f.link, link: f.link, btn: f.btn,
                              parentEl: f.parentEl, isLabel: false};
  }

  // ==== scan：接收已选层 → 行 → 列统计 ====
  window.__pp_scan = function (group) {
    if (!group || !group.records || !group.records.length) return null;
    const records = group.records;
    const subSem = findSubSem(records);
    const rows = [];
    for (const rec of records) {
      const items = subSem ? itemRootsOf(rec, subSem) : [];
      const rFields = fieldsOf(rec, items);
      if (!items.length) {
        const map = {};
        for (const f of rFields) rowPut(map, f);
        rows.push({map: map, fields: rFields});
      } else for (const it of items) {
        const map = {};
        for (const f of rFields) rowPut(map, f);
        const iFields = fieldsOf(it, null);
        for (const f of iFields) rowPut(map, f);
        rows.push({map: map, fields: rFields.concat(iFields)});
      }
    }
    const total = rows.length;
    // 键元信息（首个出现的字段）：按钮/标签/父容器；规则 2 紧挨标签命名
    const keyInfo = {}, order = [];
    const labelKeys = {};
    if (rows.length) {
      for (const key in rows[0].map) {  // map 键序 = 主列/链接列相邻（插入序）
        if (!keyInfo[key]) {
          const f = rows[0].map[key];
          keyInfo[key] = {btn: f.btn, isLabel: f.isLabel,
                          parentEl: f.parentEl, labelName: null};
          order.push(key);
        }
      }
      const fields = rows[0].fields;
      for (let i = 1; i < fields.length; i++) {
        const f = fields[i], p = fields[i - 1];
        const ki = keyInfo[f.key];
        if (!ki || ki.labelName || !keyInfo[p.key]) continue;
        if (p.isLabel && keyInfo[p.key].parentEl === f.parentEl) {
          ki.labelName = norm(p.value.replace(/[:：]\\s*$/, ""));
          labelKeys[p.key] = true;  // 标签列本身删除（C1 规则②）
        }
      }
    }
    const mem = window.__pp_colnames || {};
    const columns = [], dropped = [], usedNames = {};
    const nameHits = {n: 0};
    const isLinkKey = k => k.slice(k.length - 3) === "_链接";
    const lastSemOf = k => k.split(">").pop().replace(SEM_IDX_RE, "");
    const autoName = (key, vals) => {  // ②紧挨标签 ③值前缀 ④词典；⑤在外层兜底
      const ki = keyInfo[key] || {};
      if (ki.labelName) return ki.labelName;
      const nonEmpty = vals.filter(v => v);
      const pn = prefixName(nonEmpty);
      if (pn) return pn.name;
      return dictName(lastSemOf(key));
    };
    for (const key of order) {
      const n = rows.filter(r => r.map[key]).length;
      if (n * 2 < total || labelKeys[key]) {
        dropped.push({key: key, n: n});  // 频次不足，或被规则②当作标签删除
        continue;
      }
      const vals = rows.map(r => (r.map[key] || {}).value || "");
      const nonEmpty = vals.filter(v => v);
      const ki = keyInfo[key] || {};
      const isLink = isLinkKey(key);
      const base = isLink
        ? columns.find(c => c.key === key.slice(0, key.length - 3)) : null;
      const pn = isLink ? null : prefixName(nonEmpty);
      const lastSem = isLink ? (base ? base.sem : "") : lastSemOf(key);
      const auto = isLink
        ? (base ? base.auto_name + "链接" : null)
        : autoName(key, vals);                                   // ②③④
      const fallback = "列" + (columns.length + 1);
      let name = mem[key]                                        // ① 记住的名字
        || auto
        || (isLink && base ? base.name + "链接" : null)
        || fallback;                                             // ⑤ 兜底
      if (usedNames[name] !== undefined) name += "#" + (++usedNames[name]);
      else usedNames[name] = 1;
      if (name === mem[key]) nameHits.n++;
      let sample = "";
      if (isLink) sample = nonEmpty[0] || "";
      else if (pn) {
        for (const v of nonEmpty)
          if (v.indexOf(pn.strip) === 0) { sample = norm(v.slice(pn.strip.length)); break; }
      }
      if (!sample) sample = nonEmpty.length ? nonEmpty[0] : "";
      const btn = isLink ? !!(base && base.btn) : !!ki.btn;
      columns.push({key: key, name: name, renamed: !!mem[key],
                    auto_name: auto || (isLink && base ? base.name + "链接"
                                                       : fallback),
                    strip: (!isLink && pn) ? pn.strip : null,
                    sem: lastSem, sample: sample, btn: btn,
                    default_checked: !btn && !sameAll(vals)});
    }
    const toRow = r => {
      const o = {};
      for (const c of columns) {
        const f = r.map[c.key];
        let v = f ? f.value : "";
        if (c.strip && v.indexOf(c.strip) === 0) v = norm(v.slice(c.strip.length));
        o[c.name] = v;
      }
      return o;
    };
    const sampleRows = rows.slice(0, 3).map(toRow);
    const sampleKeys = rows.slice(0, 3).map(r => {
      const o = {};
      for (const k in r.map) o[k] = r.map[k].value || "";
      return o;
    });
    return {n_records: records.length, record_sem: group.recordSem,
            container_selector: containerSelector(group.container),
            record_selector: recordSelector(group.container, records[0]),
            sub_sem: subSem, n_items: subSem ? total : 0,
            named_hits: nameHits.n,
            columns: columns, sample_rows: sampleRows, sample_keys: sampleKeys,
            dropped: dropped};
  };
  window.__pp_groups = groupsOf;
  window.__pp_default = defaultGroup;
  window.__pp_group2 = scanGroup2;  // 两点探组：覆层 Shift+点第二条消歧

  // ==== extract：按 spec 抽全部行（与 scan 同一条 fieldsOf 管线）====
  window.__pp_extract = function (spec) {
    const cols = spec.columns || [], subSem = spec.sub_sem || null, out = [];
    for (const rec of document.querySelectorAll(spec.record_selector)) {
      let items = subSem ? itemRootsOf(rec, subSem) : [];
      if (!items.length) items = [null];
      for (const it of items) {
        const map = {};
        for (const f of fieldsOf(rec, items)) rowPut(map, f);
        if (it) for (const f of fieldsOf(it, null)) rowPut(map, f);
        const row = {};
        for (const c of cols) {
          const isLink = c.key.slice(c.key.length - 3) === "_链接";
          const base = isLink ? c.key.slice(0, c.key.length - 3) : c.key;
          const f = map[base];
          let v = "";
          if (isLink) v = (f && f.link) ? f.link : "";
          else {
            v = f ? f.value : "";
            if (c.strip && v.indexOf(c.strip) === 0) v = norm(v.slice(c.strip.length));
          }
          row[c.name] = v;
        }
        out.push(row);
      }
    }
    return out;
  };
})();
"""
