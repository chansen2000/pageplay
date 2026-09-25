"""run 执行器：页面风控检查、表格/卡片提取、结果落盘、文件下载。

供 run 流程按任务书驱动：每拿到一页响应先过 check_page_risk，再按
选择器抽表（语义表格）或抽卡片（div 卡片列表）、抽完即落盘，需要导出
文件时走 download_element。本模块不持有会话状态——context 由调用方
（session/run 层）提供。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from .guard import Guard

logger = logging.getLogger(__name__)

# 页面上下文执行的读表脚本：返回 {headers, data}，值为 trim 后的文本。
# 列名取 thead 首行；无 thead 时取首行，其余行作数据。
_TABLE_JS = """
(el, arg) => {
  const all = Array.from(el.querySelectorAll("tr"));
  if (all.length === 0) return {headers: [], data: []};
  const thead = el.querySelector("thead");
  let headerTr, bodyTrs;
  if (thead) {
    headerTr = thead.querySelector("tr");
    const tbody = el.querySelector("tbody");
    bodyTrs = tbody
      ? Array.from(tbody.querySelectorAll("tr"))
      : all.filter(tr => tr !== headerTr);
  } else {
    headerTr = all[0];
    bodyTrs = all.slice(1);
  }
  const headers = Array.from(headerTr.querySelectorAll("th,td"))
    .map(c => c.textContent.trim());
  const data = bodyTrs.map(tr =>
    Array.from(tr.querySelectorAll("td,th"))
      .map(c => c.textContent.trim()));
  return {headers, data};
}
"""


# 页面上下文执行的读卡片脚本：容器内按同签名(tag+首class)兄弟重算最大组
# （≥3 条）为记录，逐条按各 field.rel（[{tag,nth}] 相对链，item 端在首）
# 解析到文本宿主元素取 trim 文本。返回 [{label: 值}, ...]；无组返回 []。
_CARDS_JS = """
(el, fields) => {
  let best = null;
  for (const c of el.children) {
    const g = Array.prototype.filter.call(el.children, s =>
      s.tagName === c.tagName && s.classList[0] === c.classList[0]
      && (s.textContent || "").trim());
    if (g.length >= 3 && (!best || g.length > best.length)) best = g;
  }
  if (!best) return [];
  return best.map(item => {
    const row = {};
    for (const f of fields) {
      let n = item;
      for (const step of (f.rel || [])) {
        let k = 0, found = null;
        for (const ch of n.children) {
          if (ch.tagName.toLowerCase() === step.tag && ++k === step.nth) {
            found = ch; break;
          }
        }
        n = found;
        if (!n) break;
      }
      row[f.label] = n ? (n.textContent || "").trim() : "";
    }
    return row;
  });
}
"""


def check_page_risk(text: str) -> None:
    """复用 guard 的风控关键词扫描：命中即 raise RiskTriggered。

    内部自建 Guard(curfew=None)——夜间禁跑约束对抓取流程不适用，
    宵禁在此关闭；关键词表沿用 guard 默认（滑块/验证码/频率限制等）。
    """
    Guard(curfew=None).check_response(text)


def extract_table(page, selector: str, columns: list[str] | None) -> list[dict]:
    """在页面上下文读 selector 指向的表格，返回 [{"列名": "值"}, ...]。

    第一行（thead 首行，无 thead 则表体首行）作列名，其余行作数据；
    单元格取文本并去除首尾空白。columns 给定时只保留这些列（顺序按
    columns），出现表中不存在的列名 → ValueError 点名缺的列。
    """
    raw = page.eval_on_selector(selector, _TABLE_JS)
    headers: list[str] = list(raw["headers"])
    data: list[list[str]] = raw["data"]
    if columns is not None:
        missing = [name for name in columns if name not in headers]
        if missing:
            raise ValueError(
                f"表中不存在这些列：{ '、'.join(missing) }（实际列：{headers}）")
        keep = [headers.index(name) for name in columns]
        headers = [headers[i] for i in keep]
        data = [[row[i] for i in keep] for row in data]
    rows: list[dict] = []
    for row in data:
        entry: dict = {}
        for i, name in enumerate(headers):
            entry[name] = row[i] if i < len(row) else ""
        rows.append(entry)
    logger.debug("extract_table %s: %d 行 x %d 列", selector, len(rows), len(headers))
    return rows


def extract_cards(page, list_selector: str, fields: list[dict]) -> list[dict]:
    """在页面上下文读 selector 指向的卡片列表，返回 [{"字段": "值"}, ...]。

    卡片列表 = div 等容器内一组同签名（同标签且首个 class 相同）兄弟
    记录（≥3 条），典型如淘宝订单列表——非语义表格，extract_table 读
    不到。fields 来自框选确认 payload（每项 {"label", "rel"}，rel 是
    记录内定位文本宿主的 [{tag,nth}] 相对链）；键用 label（框选时取
    文本前 8 字）。记录内某字段定位落空 → 该格填 ""（对齐 extract_table
    缺列补空语义）。

    fields 为空 → ValueError（至少勾选一个字段）；容器内识别不到同
    签名记录组 → ValueError 人话（回页面重新框选）。
    """
    if not fields:
        raise ValueError(
            "fields 为空：卡片抓取至少要勾选一个字段"
            "（fields=[{\"label\", \"rel\"}, ...]，来自框选确认 payload）")
    rows = page.eval_on_selector(list_selector, _CARDS_JS, fields)
    if not rows:
        raise ValueError(
            f"选择器 {list_selector!r} 下未识别到卡片列表：容器内需要"
            " ≥3 条同结构记录（同标签且首个 class 相同），请回页面重新框选")
    logger.debug("extract_cards %s: %d 行 x %d 字段",
                 list_selector, len(rows), len(fields))
    return rows


def save_table(rows: list[dict], out_dir: Path, stem: str) -> tuple[Path, Path]:
    """把提取结果落成 <stem>.csv + <stem>.json，返回 (csv, json) 路径。

    csv 用 utf-8-sig（带 BOM，Excel 直接打开不乱码），列序取首行键序；
    json 为 ensure_ascii=False 的 UTF-8。out_dir 不存在时逐级创建。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"
    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("save_table: %d 行已落盘 %s / %s", len(rows), csv_path, json_path)
    return csv_path, json_path


def download_element(page, selector: str, out_dir: Path) -> Path:
    """点击 selector 触发下载，把文件存到 out_dir 并返回落盘路径。

    文件名用浏览器给的 suggested_filename（尊重 Content-Disposition
    的 filename）；out_dir 不存在时逐级创建。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with page.expect_download() as dl_info:
        page.click(selector)
    download = dl_info.value
    target = out_dir / download.suggested_filename
    download.save_as(str(target))
    logger.info("download_element: %s 已保存 %s", download.suggested_filename, target)
    return target


def file_size_str(path: Path) -> str:
    """文件大小的人话表达："812 B" / "1.2 KB" / "3.4 MB"（1024 进位）。

    文件不存在或不可读（OSError）→ "0 B"；<1KB 不带小数，其余一位小数。
    """
    try:
        size = Path(path).stat().st_size
    except OSError:
        return "0 B"
    if size < 1024:
        return f"{size} B"
    size_f = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        size_f /= 1024
        if size_f < 1024 or unit == "TB":
            return f"{size_f:.1f} {unit}"
    return f"{size_f:.1f} TB"  # 循环必经 return，此处仅为静态检查兜底
