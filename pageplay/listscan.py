"""列表抓取 python 封装：scan 探测列 / extract 抽行 / sem_selector 造选择器。

引擎算法全在 _listscan_js.LISTSCAN_JS（页面上下文执行，设计 §3），本模块
只负责注入、调用与日志：一次 scan 一条 INFO（记录数/子项数/保留列数），
丢弃列及出现频率记 DEBUG（设计 §7），日志带 [LIST] 前缀供 grep 追踪。
本模块不持有会话状态——page 由调用方（会话/run 层）提供。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import _listscan_js

logger = logging.getLogger(__name__)

# 常见标签名：sem_selector 里用于区分"裸标签段"与"语义 class 段"
_TAGS = frozenset({
    "html", "body", "div", "span", "a", "p", "ul", "ol", "li", "table",
    "thead", "tbody", "tr", "td", "th", "section", "article", "header",
    "footer", "main", "nav", "aside", "form", "button", "input", "label",
    "img", "h1", "h2", "h3", "h4", "h5", "h6",
})


def _inject(page) -> None:
    """把 listscan JS 注入页面（幂等：重复注入覆盖同名入口，无副作用）。"""
    page.evaluate(_listscan_js.LISTSCAN_JS)


_PICK_DEFAULT_JS = """
(el) => {
  const gs = window.__pp_groups(el);
  const g = window.__pp_default(gs);
  return {scan: g ? window.__pp_scan(g) : null,
          n_layers: gs.length,
          n_records: g ? g.records.length : 0};
}
"""


def scan(page, record_selector: str, colnames: dict | None = None) -> dict:
    """页面上第一个匹配 record_selector 的记录元素 → 跨记录列统计。

    选层走任务书 v2 A：__pp_groups 收全部候选层 → __pp_default 取默认层
    （记录数最多，同数取内层）→ __pp_scan 只扫已选层。colnames 给定时
    注入 window.__pp_colnames（本站记住的列名，C1 规则 ①）。

    返回 scan 产物全量：{columns: [{key, name, auto_name, strip, sem,
    sample, btn, default_checked}], sample_rows, n_records, n_items,
    record_sem, record_selector, container_selector, sub_sem, named_hits,
    dropped: [{key, n}], n_layers}。选择器无匹配 / 识别不到 ≥3 条同 sem
    记录 → ValueError 人话（回页面重新框选）。
    """
    _inject(page)
    if colnames:
        page.evaluate("c => window.__pp_colnames = c", colnames)
    raw = page.eval_on_selector(record_selector, _PICK_DEFAULT_JS)
    if raw is None or raw.get("scan") is None:
        raise ValueError(
            f"选择器 {record_selector!r} 在页面上没有匹配到元素，或该处"
            "没有 ≥3 条同结构记录（同语义 class 的兄弟），请回页面重新框选")
    res = raw["scan"]
    res["n_layers"] = raw["n_layers"]
    logger.info(
        "[LIST] scan %s：%d 层默认 %d 条记录 / %d 个子项 / 保留 %d 列"
        "（丢弃 %d 列，改名记忆命中 %d）",
        record_selector, raw["n_layers"], res["n_records"], res["n_items"],
        len(res["columns"]), len(res.get("dropped", [])),
        res.get("named_hits", 0))
    for d in res.get("dropped", []):
        logger.debug("[LIST] 丢弃列 %s（仅 %d/%d 行出现）",
                     d["key"], d["n"], res["n_items"] or res["n_records"])
    return res


def extract(page, spec: dict) -> list[dict]:
    """按 scan 产物的 spec（record_selector/sub_sem/columns）抽全部行。

    columns 每项至少 {key, name}，可选 strip（去前缀）；key 带 _链接
    后缀的列取该字段宿主所在 a[href] 的绝对 URL。返回 [{"列名": "值"}]。
    """
    _inject(page)
    rows = page.evaluate("(spec) => window.__pp_extract(spec)", spec)
    logger.debug("[LIST] extract：%d 行 x %d 列",
                 len(rows), len(spec.get("columns", [])))
    return rows


def sem_selector(chain: list[str]) -> str:
    """语义链 → CSS 选择器（纯函数，容器/记录选择器统一出口）。

    链段两种：标签段（div 等常见 tag，修饰紧随其后的 class 段，合成
    "div.xxx" 形态）；class 段取 "--" 前语义名（不写哈希）——末段（目标
    记录）与带 "--" 的段用宽松 [class*="语义名"]（§6 形态，哈希有无都
    命中），其余 class 段用 .语义名。与 _listscan_js.selLink 同源。

    >>> sem_selector(["div", "trade-content-container", "trade-container"])
    'div.trade-content-container > [class*="trade-container"]'
    """
    parts: list[str] = []
    pending = ""  # 未落位的标签段
    for i, raw in enumerate(chain):
        seg = (raw or "").strip()
        if not seg:
            continue
        if seg in _TAGS:
            pending = seg
            continue
        hashed = "--" in seg
        base = seg.split("--", 1)[0] if hashed else seg
        if i == len(chain) - 1 or hashed:
            link = f'[class*="{base}"]'
        else:
            link = f".{seg}"
        parts.append(pending + link if pending else link)
        pending = ""
    if pending:
        parts.append(pending)
    return " > ".join(parts)


def _colnames_path(site_label: str) -> Path:
    """列名记忆文件路径：<~/.pageplay 或 $PAGEPLAY_HOME>/sites/<站>/colnames.json。"""
    base = Path(os.environ.get("PAGEPLAY_HOME", "~/.pageplay")).expanduser()
    return base / "sites" / site_label / "colnames.json"


def load_colnames(site_label: str) -> dict:
    """读本站记住的列名（C1 规则 ①）；文件缺失/损坏 → 空表。"""
    try:
        data = json.loads(_colnames_path(site_label).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_colnames(site_label: str, columns: list[dict]) -> int:
    """确认时对比 name vs auto_name，不同的写 colnames.json，返回写入数。

    覆盖同 key（用户改名以最新为准）；文件不存在逐级创建；写入失败不
    抛（记忆是增强能力，不能挡住抓取本身），只 log.warning。
    """
    diffs = {c["key"]: c["name"] for c in columns
             if c.get("key") and c.get("name")
             and c.get("auto_name") and c["name"] != c["auto_name"]}
    if not diffs:
        return 0
    path = _colnames_path(site_label)
    data = load_colnames(site_label)
    data.update(diffs)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as exc:
        logger.warning("[LIST] 列名记忆写入失败（%s）：%s", path, exc)
        return 0
    logger.info("[LIST] 列名记忆已更新 %s：%d 个新名字", path, len(diffs))
    return len(diffs)
