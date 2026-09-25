"""Tkinter 控制台：GUI 只是壳，实际执行同一套 pageplay CLI（sheng 拍板）。

本文件不实现任何引擎逻辑：每个按钮经 build_command 拼出 argv，子进程跑
与终端完全相同的 pageplay 命令（cli.main），输出逐行回读进日志窗；不改
引擎行为，PAGEPLAY_HOME 等环境变量原样透传（仅追加 PYTHONUNBUFFERED）。
线程模型（tkinter 唯一安全做法）：后台 daemon 线程逐行读 stdout 入
queue，主线程 after(100ms) 轮询刷 UI，控件只在主线程碰；运行期互斥按钮
全 disabled，结束恢复并自动刷新下拉（--json 机读数组）。「取消」= SIGINT
安全收尾（T13 契约，详见 _cancel / _on_close）。「录制」名称留空自动起
默认名并回填输入框（T13.3 防丢）。数据预览（v0.7-B）整体在
gui_preview.py，本模块回导其全部旧名；预览触发 = 账本新增记录才弹
（_launch 记行数基线 → _on_done 比对 should_preview）。T18 起布局改
向导式四段（sheng 拍板"按使用顺序组织界面"），按钮与 op 映射不变。
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import scrolledtext, ttk

from .gui_preview import (_count_runs, _runs_jsonl_path, latest_csv_output,
                          load_csv_for_preview, open_preview_window,
                          should_preview)  # 既有 pageplay.gui.* 名字照旧可用

# 子进程命令前缀：同一解释器、同一 CLI 入口（cli.main），退出码原样透传。
# 用 -c 而不是 -m，因为 cli.py 没有也不需要 __main__ 块。
_CLI_PREFIX = [sys.executable, "-c",
               "import sys; from pageplay.cli import main; sys.exit(main())"]

# 仓库根：未安装（python -m pageplay.gui 直接从源码跑）时保证子进程
# 能 import 到本包；已安装场景下 cwd 在此也不影响行为。
_REPO_ROOT = Path(__file__).resolve().parent.parent

# record 等命令要交互的路径都已有 EOFError 兜底（T13.1 自动命名保存）；
# GUI 录制总是自动起名传 --name，起名交互在 GUI 里永远不出现
_REQUIRES_SITE = frozenset({"login", "doctor", "open", "record", "export"})
_OPERATIONS = frozenset({
    "login", "doctor", "open", "record", "run", "export",
    "results", "flows", "recipes", "shutdown", "grab", "vision",
})


def build_command(op: str, params: dict | None = None) -> list[str]:
    """把 GUI 操作翻译成完整 pageplay argv（纯函数，单测锚点）。

    op ∈ {login, doctor, open, record, run, export, results, flows,
    recipes, shutdown, grab, vision}；params 只认三个键：site_or_url（顶部
    站点/网址输入框）、name（名称输入框，record 时作 --name；run 时作
    重放名）、headless（run 时作 --headless）。多余的键忽略；非法 op →
    ValueError。
    - login/doctor/open/record/export 必填 site_or_url，缺 → ValueError
    - grab/vision 站点可选（给了才传：仅用于命名与落账归属）
    - run 必填 name（下拉选中的流程/recipe 名），缺 → ValueError
    - record 有 name 才传 --name（GUI 侧由 next_record_name 保证非空）
    - run 有 headless 才传 --headless
    """
    if op not in _OPERATIONS:
        raise ValueError(
            f"未知操作：{op!r}（可用：{', '.join(sorted(_OPERATIONS))}）")
    params = dict(params or {})
    site = str(params.get("site_or_url") or "").strip()
    name = str(params.get("name") or "").strip()
    if op in _REQUIRES_SITE and not site:
        raise ValueError(f"{op} 需要站点或网址：请先在顶部填写")
    if op == "run" and not name:
        raise ValueError("重放需要流程或 recipe 名：请先在下拉框选择（或点「刷新」）")
    argv = [op]
    if op in _REQUIRES_SITE:
        argv.append(site)
    if op in ("grab", "vision") and site:  # 站点可选：给了才传
        argv.append(site)
    if op == "run":
        argv.append(name)
        if params.get("headless"):
            argv.append("--headless")
    elif op == "record" and name:
        argv += ["--name", name]
    return argv


def next_record_name(site: str, known_flows) -> str:
    """录制缺名时的默认流程名（T13.3 防丢）：<站>-flow-<N>。

    站点标签与 CLI parse_target 同近似（www.taobao.com → taobao）；
    N 从已知流程清单（flows --json 的产物，refresh_names 维护）里同
    前缀最大号 +1；清单里没有同前缀（取不到 N）→ 时间戳后 4 位兜底。
    站点推不出标签（空串等）返回 ""：交 build_command 按缺站点报错。
    """
    host = site.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/")[0].split(":")[0]
    parts = [p for p in host.split(".") if p]
    label = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")
    if not label:
        return ""
    prefix = f"{label}-flow-"
    max_no = 0
    for flow_name in known_flows:
        matched = re.fullmatch(re.escape(prefix) + r"(\d+)", str(flow_name))
        if matched:
            max_no = max(max_no, int(matched.group(1)))
    if max_no:
        return f"{prefix}{max_no + 1}"
    return f"{prefix}{datetime.now().strftime('%Y%m%d%H%M%S')[-4:]}"


class App:
    """主窗口：向导式四段布局（T18，sheng 拍板"按使用顺序组织界面"）。

    自上而下 = 一次完整使用的先后顺序：第 1 步登录网站（每站一次）→
    第 2 步抓数据（2A 临时抓一页 / 2B 录制反复抓，两条路选一条）→
    第 3 步看结果 → 管理（低频）；底部状态栏 + 日志窗照旧。每段一个
    LabelFrame；按钮文字带序号（① 登录 / ② 录制 / ③ 取当前页 /
    ④ 重放），按钮 → op 映射与 build_command 一字未动，只重新摆放。

    运行模型：_launch 起子进程与读线程 → _pump 逐行入队 → _poll 主线程
    消费刷日志/状态。队列元素：str = 日志行；tuple = ("done", 码)
    或 ("names", 来源, 名单)。
    """

    POLL_MS = 100
    PREVIEW_ROWS = 500  # 预览窗最多展示的行数（v0.7-B）

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self._runs_before = 0  # 账本行数基线（_launch 记，_on_done 比对）
        self._buttons: dict[str, ttk.Button] = {}
        self._names: dict[str, list[str]] = {}  # {"flows": […], "recipes": […]}
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(self.POLL_MS, self._poll)
        self.refresh_names()

    # ---- 布局（向导四段）----------------------------------------------

    def _build_ui(self) -> None:
        body = ttk.Frame(self.root)
        body.pack(fill="x", padx=8, pady=(8, 4))

        def add_button(parent: ttk.Frame, label: str, op: str) -> None:
            """造操作按钮：文字带序号，op 映射与互斥登记照旧。"""
            btn = ttk.Button(parent, text=label,
                             command=lambda op=op: self._launch(op))
            btn.pack(side="left", padx=3, pady=2)
            self._buttons[label] = btn

        # 第 1 步：登录网站（每个网站只需一次）
        step1 = ttk.LabelFrame(
            body, text="第 1 步：登录网站（每个网站只需一次）")
        step1.pack(fill="x", padx=2, pady=(2, 4))
        ttk.Label(step1, text="站点/网址").pack(side="left", padx=(6, 0))
        self.site_var = tk.StringVar()
        ttk.Entry(step1, textvariable=self.site_var, width=22).pack(
            side="left", padx=(4, 12), pady=3)
        add_button(step1, "① 登录", "login")
        add_button(step1, "验活", "doctor")

        # 第 2 步：抓数据（两条路选一条）
        step2 = ttk.LabelFrame(body, text="第 2 步：抓数据（两条路选一条）")
        step2.pack(fill="x", padx=2, pady=4)
        ttk.Label(step2, text="2A 临时抓一页：打开窗口 → 逛到目标页 → 取当前页"
                  ).pack(anchor="w", padx=(6, 0))
        ttk.Label(step2, text="2B 反复自动抓：录制 → 逛+按P框选 → 关窗起名"
                  ).pack(anchor="w", padx=(6, 0))
        row_a = ttk.Frame(step2)
        row_a.pack(fill="x", pady=(2, 0))
        add_button(row_a, "打开窗口", "open")
        add_button(row_a, "③ 取当前页", "grab")
        add_button(row_a, "视觉抓取", "vision")
        add_button(row_a, "② 录制", "record")
        add_button(row_a, "④ 重放", "run")
        row_b = ttk.Frame(step2)
        row_b.pack(fill="x", pady=(2, 3))
        ttk.Label(row_b, text="名称").pack(side="left", padx=(6, 0))
        self.name_var = tk.StringVar()
        ttk.Entry(row_b, textvariable=self.name_var, width=14).pack(
            side="left", padx=(4, 12))
        ttk.Label(row_b, text="流程/recipe").pack(side="left")
        self.pick_var = tk.StringVar()
        self.pick_box = ttk.Combobox(row_b, textvariable=self.pick_var,
                                     width=20, values=[])
        self.pick_box.pack(side="left", padx=(4, 8))
        self._buttons["刷新"] = ttk.Button(row_b, text="刷新",
                                           command=self.refresh_names)
        self._buttons["刷新"].pack(side="left", padx=3)
        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_b, text="无头重放",
                        variable=self.headless_var).pack(side="left", padx=(12, 0))

        # 第 3 步：看结果
        step3 = ttk.LabelFrame(body, text="第 3 步：看结果")
        step3.pack(fill="x", padx=2, pady=4)
        add_button(step3, "结果", "results")
        add_button(step3, "流程列表", "flows")
        add_button(step3, "recipe列表", "recipes")
        add_button(step3, "导出", "export")

        # 管理（低频）
        admin = ttk.LabelFrame(body, text="管理（低频）")
        admin.pack(fill="x", padx=2, pady=(4, 2))
        add_button(admin, "关闭守护", "shutdown")

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8)
        self.status_var = tk.StringVar(value="空闲")
        ttk.Label(bar, textvariable=self.status_var,
                  anchor="w").pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(bar, text="取消", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="right")

        self.log = scrolledtext.ScrolledText(self.root, height=14,
                                             state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))

    # ---- 运行模型：起子进程 → 后台读 → 主线程刷------------------------

    def _launch(self, op: str) -> None:
        """按钮统一入口：拼 argv → 起子进程 → 进入互斥运行态。"""
        params = {"site_or_url": self.site_var.get(),
                  "name": self.name_var.get(),
                  "headless": bool(self.headless_var.get())}
        if op == "run":  # 重放：下拉选中值即名字（先流程后 recipe 由 CLI 双查）
            params["name"] = self.pick_var.get()
        if op == "record" and not params["name"] and params["site_or_url"]:
            # T13.3 防丢：名称留空自动起默认名并回填输入框——record 必带
            # --name，CLI 的起名交互在 GUI 里永远不出现
            auto = next_record_name(str(params["site_or_url"]),
                                    self._names.get("flows", []))
            if auto:
                params["name"] = auto
                self.name_var.set(auto)
        try:
            argv = build_command(op, params)
        except ValueError as exc:
            self._log(f"未执行：{exc}")
            return
        self._log(f"$ pageplay {' '.join(argv)}")
        env = dict(os.environ)  # PAGEPLAY_HOME 等原样透传
        env["PYTHONUNBUFFERED"] = "1"  # 子进程 print 即时到达日志窗
        try:
            self.proc = subprocess.Popen(
                _CLI_PREFIX + argv, cwd=_REPO_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env)
        except OSError as exc:
            self.proc = None
            self._log(f"启动失败：{exc}")
            return
        self.cancelled = False
        self._runs_before = _count_runs(_runs_jsonl_path())  # 预览闸门基线
        self._set_running(True)
        threading.Thread(target=self._pump, args=(self.proc,),
                         daemon=True).start()
        if op == "record":  # T13：录制怎么停、录成什么名，先说清楚
            self._log("录制中：正常浏览即记录，按 P 框选收获")
            self._log("停止：关闭浏览器里该标签页，或点「取消」（会安全保存）")
            self._log(f"流程名将用：{params['name'] or '（回车默认）'}")

    def _pump(self, proc: subprocess.Popen) -> None:
        """后台线程：逐行读子进程 stdout 入队；结束后投递 ("done", 码)。
        线程里只碰 queue，不碰任何 tkinter 控件。
        """
        assert proc.stdout is not None
        for line in proc.stdout:
            self.queue.put(line.rstrip("\n"))
        self.queue.put(("done", proc.wait()))

    def _poll(self) -> None:
        """主线程消费队列：日志行刷窗，控制消息改状态/下拉。"""
        try:
            while True:
                item = self.queue.get_nowait()
                if isinstance(item, str):
                    self._log(item)
                elif item[0] == "done":
                    self._on_done(int(item[1]))
                elif item[0] == "names":
                    self._on_names(str(item[1]), list(item[2]))
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._poll)

    def _on_done(self, code: int) -> None:
        self._set_running(False)
        self.proc = None
        if self.cancelled:
            self._log("— 已被用户取消 —")
            self.status_var.set("已取消")
        else:
            self._log(f"— 退出码 {code} —")
            self.status_var.set(f"空闲（上次退出码 {code}）")
            if should_preview(self._runs_before, _count_runs(_runs_jsonl_path())):
                self._maybe_preview()  # v0.7 收紧：账本有新增且末条带 CSV 才弹
        self.refresh_names()  # 结束自动刷新下拉（--json 机读）

    def _cancel(self) -> None:
        """请求停止：SIGINT（POSIX 等同 Ctrl-C）让命令安全收尾——record
        走 KeyboardInterrupt 路径把已录步骤存成流程再退（T13）。3s 后
        仍活着才升级 terminate（收尾卡死兜底）。"""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self.cancelled = True
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass  # 恰好已自己退出，done 消息稍后照常到达
        self._log("已请求停止（等待安全收尾）…")
        self.root.after(3000, self._escalate, proc)

    def _escalate(self, proc: subprocess.Popen) -> None:
        """3s 宽限到点：仍是同一子在跑才强杀（不误杀后来的新任务）。"""
        if proc is not self.proc or proc.poll() is not None:
            return  # 已退出或已被新任务取代：无事可做
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        self._log("收尾超时，已强制终止")

    def _set_running(self, running: bool) -> None:
        """互斥：运行中全部按钮 disabled（取消钮除外），状态栏提示。"""
        for btn in self._buttons.values():
            btn.state(["disabled"] if running else ["!disabled"])
        self.cancel_btn.state(["!disabled"] if running else ["disabled"])
        if running:
            self.status_var.set("执行中…")

    # ---- 日志窗与下拉刷新----------------------------------------------

    def _log(self, line: str) -> None:
        """追加一行（时间戳前缀）并滚动跟底；日志窗常态只读。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {line}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_names(self) -> None:
        """后台拉 flows/recipes --json 回填下拉；失败静默保持旧值。"""
        for op in ("flows", "recipes"):
            threading.Thread(target=self._fetch_names, args=(op,),
                             daemon=True).start()

    def _fetch_names(self, op: str) -> None:
        """后台线程：跑一条清单命令的 --json，解析出名字列表入队。"""
        try:
            cmd = _CLI_PREFIX + build_command(op) + ["--json"]
            done = subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True,
                                  text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
            names = [str(row["name"]) for row in json.loads(done.stdout)]
        except (OSError, ValueError, KeyError, TypeError):
            return  # 静默：下拉保持旧值，不因刷新失败打扰
        self.queue.put(("names", op, names))

    def _on_names(self, op: str, names: list[str]) -> None:
        self._names[op] = names
        # flows 与 recipes 合并展示：run 双查两种名字都能重放
        merged = sorted(set(self._names.get("flows", []))
                        | set(self._names.get("recipes", [])))
        self.pick_box["values"] = merged

    # ---- 数据预览（v0.7-B）：命令结束后账本有新增且带 CSV 才弹----------

    def _maybe_preview(self) -> None:
        """收获预览入口：账本最新 ok 记录里有 .csv → 弹预览窗 + 记日志。
        只在 _on_done（子进程已结束、账本有新增）里调用；max_rows+1 多
        读一行探截断，读到了才标"仅显示前 N 行"。预览是锦上添花：失败
        只记一行日志，不弄崩控制台。
        """
        try:
            csv_path = latest_csv_output(_runs_jsonl_path())
            if not csv_path:
                return
            columns, rows = load_csv_for_preview(
                csv_path, max_rows=self.PREVIEW_ROWS + 1)
        except ValueError as exc:
            self._log(f"数据预览跳过：{exc}")
            return
        truncated = len(rows) > self.PREVIEW_ROWS
        rows = rows[:self.PREVIEW_ROWS]
        note = f"，仅显示前 {self.PREVIEW_ROWS} 行" if truncated else ""
        self._log(f"已弹出数据预览：{Path(csv_path).name}（{len(rows)} 行{note}）")
        self._open_preview(csv_path, columns, rows, truncated)

    def _open_preview(self, csv_path: str, columns: list[str],
                      rows: list[list[str]], truncated: bool) -> None:
        """建只读预览窗：实现整体在 gui_preview.open_preview_window
        （T18 拆分），这里只传 root 与行数上限，保持旧方法名可用。"""
        open_preview_window(self.root, csv_path, columns, rows, truncated,
                            self.PREVIEW_ROWS)

    def _on_close(self) -> None:
        """退出 GUI：子进程还在跑就先 SIGINT（安全收尾，录制不丢）再关窗。
        关窗后子进程成为孤儿继续把收尾做完（流程落盘），无人再杀它。
        """
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        self.root.destroy()


def main() -> None:
    """GUI 入口：只有这里才建 Tk root 起主循环（import 本模块零副作用）。"""
    root = tk.Tk()
    root.title("pageplay 控制台")
    root.geometry("780x680")  # T18 向导四段比旧三段高，日志窗照旧 expand
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
