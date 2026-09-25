"""GUI 数据预览层（v0.7-B 引入，T18 从 gui.py 整体拆出）。

职责单一：命令结束后从执行账本找出 CSV 收获并弹只读预览窗。
- should_preview / _count_runs：预览触发闸门纯函数（账本行数有增长才弹）；
- latest_csv_output：账本末尾向前扫最后一条有效记录的 .csv 产物；
- load_csv_for_preview：CSV → (表头, 行) 全文本预览数据（pandas 延迟导入）；
- open_preview_window：建只读 Treeview 预览窗（原 App._open_preview，
  root 与 preview_rows 参数化后搬出类，行为零变化）。
gui.py `from .gui_preview import ...` 回导全部旧名（含下划线名），既有
`pageplay.gui.latest_csv_output` 等 import 路径不变（测试零改动为证）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from .actions import file_size_str


def _runs_jsonl_path() -> Path:
    """执行账本路径（GUI 只读不写）：与 cli_pick 同款，PAGEPLAY_HOME 覆盖。"""
    return Path(os.environ.get("PAGEPLAY_HOME", "~/.pageplay")
                ).expanduser() / "runs.jsonl"


def latest_csv_output(runs_path: Path) -> str | None:
    """账本最后一条有效记录的 .csv 产物路径；没有 → None。

    从文件末尾向前找第一条能解析的记录（坏行——JSON 损坏/非对象——
    跳过继续向前）；status=ok 且 outputs 里有 .csv（取最后一个）→
    返回该路径，否则 None（fail / ok 无 csv / 不存在 / 空文件）。
    只认最后一条有效记录，不回看更旧的收获。
    """
    runs_path = Path(runs_path)
    if not runs_path.is_file():
        return None
    text = runs_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue  # 坏行跳过
        if not isinstance(data, dict):
            continue
        outputs = data.get("outputs")
        if data.get("status") == "ok" and isinstance(outputs, list):
            for item in reversed(outputs):
                if str(item).lower().endswith(".csv"):
                    return str(item)
        return None  # 最后一条有效记录说了算
    return None


def _count_runs(runs_path: Path) -> int:
    """账本非空行数（读不了 → 0）；追加式账本：行数增长 = 新增了执行记录。"""
    try:
        text = Path(runs_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for ln in text.splitlines() if ln.strip())


def should_preview(before: int, after: int) -> bool:
    """预览闸门（纯函数）：账本行数有增长才值得弹预览（无增长 = 无新收获）。"""
    return after > before


def load_csv_for_preview(path: str, max_rows: int = 500
                         ) -> tuple[list[str], list[list[str]]]:
    """CSV → (表头, 行) 预览数据，值全文本，最多 max_rows 行。

    pandas.read_csv(dtype=str, keep_default_na=False)：编号列保持文本
    （"001" 不变形），空单元格是 ""；utf-8-sig 兼容 save_table 带 BOM
    产物。不存在/空文件/解析失败 → ValueError（人话）。超 max_rows 只
    返回前 max_rows 行；判"是否截断"用 max_rows+1 再读一次看是否多
    返回一行（GUI 用此法打标记，不猜）。
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ValueError(f"找不到 CSV 文件：{csv_path}")
    try:
        import pandas  # 延迟导入：预览用到才加载，GUI 启动不背其开销
    except ImportError as exc:
        raise ValueError(f"预览需要 pandas（当前环境未安装）：{exc}") from exc
    try:
        df = pandas.read_csv(csv_path, dtype=str, keep_default_na=False,
                             encoding="utf-8-sig", nrows=max(max_rows, 0))
    except pandas.errors.EmptyDataError as exc:
        raise ValueError(f"CSV 文件是空的：{csv_path}") from exc
    except OSError as exc:
        raise ValueError(f"读不了 CSV 文件：{csv_path}（{exc}）") from exc
    except ValueError as exc:  # 解析失败（ParserError 是 ValueError 子类）
        raise ValueError(f"不是有效的 CSV：{csv_path}（{exc}）") from exc
    columns = [str(c) for c in df.columns]
    rows = [[str(v) for v in row]
            for row in df.itertuples(index=False, name=None)]
    return columns, rows


def open_preview_window(root: tk.Misc, csv_path: str, columns: list[str],
                        rows: list[list[str]], truncated: bool,
                        preview_rows: int) -> None:
    """建只读预览窗：Treeview 表格 + 横竖滚动条 + 底部落账标签；
    每次收获弹一个新 Toplevel 互不替换，数据只读不回写 CSV。
    （原 App._open_preview：self.root → root、self.PREVIEW_ROWS →
    preview_rows，两处参数化，函数体逐行原样。）"""
    win = tk.Toplevel(root)
    win.title(f"数据预览：{Path(csv_path).name}")
    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    tree = ttk.Treeview(frame, columns=columns, show="headings", height=18)
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=110, anchor="w")
    for row in rows:  # 只读展示：插表后不回写 CSV
        tree.insert("", "end", values=row)
    ysb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ysb.grid(row=0, column=1, sticky="ns")
    xsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    label = f"已保存：{Path(csv_path).resolve()}（{file_size_str(Path(csv_path))}）"
    if truncated:
        label += f"——仅显示前 {preview_rows} 行"
    ttk.Label(win, text=label, anchor="w").pack(
        fill="x", padx=8, pady=(0, 8))
