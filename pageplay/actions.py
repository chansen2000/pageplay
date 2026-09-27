"""run 执行器：页面风控检查、表格/列表/链接提取、结果落盘、文件下载。

供 run 流程按任务书驱动：每拿到一页响应先过 check_page_risk，再按
选择器抽表（语义表格）或抽列表（listscan 引擎，v0.10 设计 §3）或抓
框内链接（§4），抽完即落盘；文件类产物走 download_element /
download_links。本模块不持有会话状态——context 由调用方（session/run
层）提供。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from urllib.parse import unquote

from . import listscan as _listscan
from .guard import Guard
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# 0 行失败的稳定锚点（T20）：extract_table 空结果 raise 的消息前缀，
# cli_pick 据此把"锁错目标"与会话退出码 1 关联，不靠文案巧合。
ZERO_ROWS_PREFIX = "提取到 0 行"

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


# 页面上下文执行的抓链接脚本：框选容器内全部 a[href] → 行 {文字, 链接,
# 下载属性}，按 href 去重（§4「只抓链接」）。
_LINKS_JS = """
(el) => {
  const out = [], seen = new Set();
  for (const a of el.querySelectorAll("a[href]")) {
    const href = a.href || "";
    if (!href || seen.has(href)) continue;
    seen.add(href);
    out.push({文字: (a.textContent || "").trim(), 链接: href,
              下载属性: a.hasAttribute("download")});
  }
  return out;
}
"""

# 「像文件」的白名单扩展名（§5）；有 download 属性的链接无条件算数
_FILE_EXTS = (".pdf", ".xls", ".xlsx", ".csv", ".zip",
              ".doc", ".docx", ".jpg", ".png")


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

    T20 防呆（消灭"抓表 0 行"误报）：columns 为空列表 → ValueError
    （一列没勾，抓不出东西）；目标里没有可读数据行（锁定容器无 tr /
    只有表头行）→ ValueError 人话（真机教训：0 行照报 ✓ 已生成，
    用户拿着空产物以为成功）。
    """
    if columns is not None and not columns:
        raise ValueError("没有勾选任何列：至少勾选一列才能抓表")
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
    if not rows or not rows[0]:  # 无数据行 / 无列名：按失败处理，不产空产物
        raise ValueError(
            f"{ZERO_ROWS_PREFIX}：锁定的目标里没有可读数据。"
            "看到「识别到 N 条记录」的面板再确认")
    logger.debug("extract_table %s: %d 行 x %d 列", selector, len(rows), len(headers))
    return rows


def extract_list(page, record_selector: str, sub_sem: str | None,
                 columns: list[dict] | None) -> list[dict]:
    """在页面上下文按 spec 抽列表（list_mode=list，v0.10 设计 §3/§6）。

    record_selector = 容器选择器 + " > " 记录级选择器（框选产出，不写
    哈希 class）；columns 来自框选确认（[{key, name, strip}]，key 是
    相对记录/子项的 sem 路径）；sub_sem 给定时每个子项一行、记录级字段
    逐行重复。行抽取与框选预览共用 listscan 的同一份 JS 管线。columns
    为空 → ValueError；0 行 → ValueError（不产空产物，同 extract_table
    语义）。
    """
    if not columns:
        raise ValueError("没有勾选任何列：至少勾选一列才能抓取")
    rows = _listscan.extract(page, {"record_selector": record_selector,
                                    "sub_sem": sub_sem, "columns": columns})
    if not rows:
        raise ValueError(
            f"{ZERO_ROWS_PREFIX}：选择器 {record_selector!r} 下没有可读"
            "数据行（页面结构可能已变化，请回页面重新框选）")
    logger.debug("extract_list %s: %d 行 x %d 列",
                 record_selector, len(rows), len(columns))
    return rows


def extract_links(page, record_selector: str) -> list[dict]:
    """框选容器内全部 a[href] → 行 {文字, 链接, 下载属性}，按 href 去重。

    选择器无匹配 → ValueError；0 条链接 → ValueError（不产空产物）。
    """
    rows = page.eval_on_selector(record_selector, _LINKS_JS)
    if rows is None:
        raise ValueError(
            f"选择器 {record_selector!r} 在页面上没有匹配到元素："
            "页面结构可能已变化，请回页面重新框选")
    if not rows:
        raise ValueError(
            f"{ZERO_ROWS_PREFIX}：框内没有可读链接，请回页面重新框选")
    logger.debug("extract_links %s: %d 条", record_selector, len(rows))
    return rows


def _file_like(url: str, has_attr: bool) -> bool:
    """§5「像文件」判定：有 download 属性，或路径扩展名在白名单。"""
    if has_attr:
        return True
    return urlsplit(url).path.lower().endswith(_FILE_EXTS)


def _filename_from_disposition(resp) -> str | None:
    """Content-Disposition 头里的 filename（含 filename* UTF-8 变体）。"""
    disp = (resp.headers or {}).get("content-disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", disp, re.I)
    if m:
        return unquote(m.group(1).strip("' "))
    m = re.search(r'filename="?([^";]+)"?', disp, re.I)
    return m.group(1).strip() if m else None


def download_links(page, links: list[dict], out_dir: Path) -> list[Path]:
    """把「像文件」的链接逐个下载到 out_dir，返回已落盘路径列表（§5）。

    links 通常直接传 extract_links 的行（含 链接/下载属性 两键）。只下
    「像文件」的链接（download 属性或白名单扩展名），其余跳过不报错；
    逐个之间 Guard.wait 护栏限速；单个失败记一行 warning 不中断。
    文件名优先 Content-Disposition，其次 URL 末段。用
    page.context.request.get（带登录 cookie），不走页面点击。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    guard = Guard(curfew=None)  # 抓取流程不适用夜间禁跑（同 check_page_risk）
    saved: list[Path] = []
    for link in links:
        url = str(link.get("链接") or link.get("url") or "")
        if not url or not _file_like(url, bool(link.get("下载属性"))):
            continue
        try:
            guard.wait()
            resp = page.context.request.get(url)
            if not resp.ok:
                logger.warning("download_links: %s 返回 %s，跳过", url, resp.status)
                continue
            name = (_filename_from_disposition(resp)
                    or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                    or f"file-{len(saved) + 1}")
            target = out_dir / name
            target.write_bytes(resp.body())
            saved.append(target)
            logger.info("download_links: %s 已保存 %s", url, target)
        except Exception as exc:  # 单个失败不中断（§5）
            logger.warning("download_links: %s 下载失败：%s", url, exc)
    return saved


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
