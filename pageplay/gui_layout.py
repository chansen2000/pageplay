"""GUI 布局（任务书 v2 E 拆分）：控件构建整体在本模块，gui.py 只留运行模型。

build_layout(app) 按「使用顺序 = 自上而下」铺控件：怎么用（三行）→ 网址
+ 登录 → 抓一次 → 以后自动抓（录制 / 重放两行）→ 结果 → 更多（默认收
起：导出登录信息 / 关闭后台浏览器 / 视觉抓取）→ 状态栏（红字报原因）→
日志窗（6 行高，可拖大）。一行一件事：行首写用途，行下一句灰字，按钮上
不写序号。按钮文字与 op 的映射见 gui.build_command；「打开结果文件夹」
是按钮=命令的唯一例外（直接打开目录，不走子进程）。

标记字符串（怎么用 / 抓这一页 / 开始录制 / 自动来一遍 / 打开结果文件夹 /
更多）是 test_gui 的防回退锚点，改名须同步改测试。
"""

import tkinter as tk
from tkinter import scrolledtext, ttk


def build_layout(app) -> None:
    """按使用顺序铺满 app.root 的全部控件；app 只被读写属性，不含布局。"""
    body = ttk.Frame(app.root)
    body.pack(fill="x", padx=8, pady=(8, 4))

    def add_button(parent: ttk.Frame, label: str, op: str) -> None:
        btn = ttk.Button(parent, text=label,
                         command=lambda op=op: app._launch(op))
        btn.pack(side="left", padx=3, pady=2)
        app._buttons[label] = btn

    def gray(parent: ttk.Frame, text: str) -> None:
        ttk.Label(parent, text=text, foreground="#888").pack(
            anchor="w", padx=(6, 0))

    # ── 怎么用（三行，一眼能懂）──
    how = ttk.LabelFrame(body, text="怎么用")
    how.pack(fill="x", padx=2, pady=(2, 4))
    for line in ("① 填网址并登录（每个网站只要一次）",
                 "② 打开浏览器，逛到要抓的页面，点「抓这一页」",
                 "③ 回到网页点一下列表 → 勾列 → 确认，表格自动弹出"):
        ttk.Label(how, text=line).pack(anchor="w", padx=(6, 0))

    # ── 网址 + ① 登录 ──
    sec_login = ttk.LabelFrame(body, text="① 登录")
    sec_login.pack(fill="x", padx=2, pady=4)
    row_url = ttk.Frame(sec_login)
    row_url.pack(fill="x", pady=(2, 0))
    ttk.Label(row_url, text="网址").pack(side="left", padx=(6, 0))
    app.site_var = tk.StringVar()
    ttk.Entry(row_url, textvariable=app.site_var, width=38).pack(
        side="left", padx=(4, 12), pady=3)
    add_button(row_url, "登录", "login")
    add_button(row_url, "检查登录", "doctor")
    gray(sec_login, "登录一次后会记住，失效了再点")

    # ── ② 抓一次 ──
    sec_once = ttk.LabelFrame(body, text="② 抓一次")
    sec_once.pack(fill="x", padx=2, pady=4)
    row_once = ttk.Frame(sec_once)
    row_once.pack(fill="x", pady=(2, 0))
    add_button(row_once, "打开浏览器", "open")
    add_button(row_once, "抓这一页", "grab")
    gray(sec_once, "点完回网页，鼠标移到列表上点一下；选错按 Esc")

    # ── ③ 以后自动抓（录制一行 + 重放一行）──
    sec_auto = ttk.LabelFrame(body, text="③ 以后自动抓")
    sec_auto.pack(fill="x", padx=2, pady=4)
    row_rec = ttk.Frame(sec_auto)
    row_rec.pack(fill="x", pady=(2, 0))
    ttk.Label(row_rec, text="录制：").pack(side="left", padx=(6, 0))
    add_button(row_rec, "开始录制", "record")
    ttk.Label(row_rec, text="名称").pack(side="left", padx=(6, 0))
    app.name_var = tk.StringVar()
    ttk.Entry(row_rec, textvariable=app.name_var, width=16).pack(
        side="left", padx=(4, 8))
    ttk.Label(row_rec, text="（可不填）").pack(side="left")
    gray(sec_auto, "正常浏览，按 P 框列表；关掉标签页就保存")
    row_run = ttk.Frame(sec_auto)
    row_run.pack(fill="x", pady=(2, 0))
    ttk.Label(row_run, text="重放：").pack(side="left", padx=(6, 0))
    ttk.Label(row_run, text="已录好的").pack(side="left", padx=(2, 0))
    app.pick_var = tk.StringVar()
    app.pick_box = ttk.Combobox(row_run, textvariable=app.pick_var,
                                width=20, values=[])
    app.pick_box.pack(side="left", padx=(4, 8))
    add_button(row_run, "自动来一遍", "run")
    app.headless_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(row_run, text="不弹浏览器",
                    variable=app.headless_var).pack(side="left", padx=(8, 0))
    gray(sec_auto, "选好名字点「自动来一遍」，整段流程自动重放")

    # ── ④ 结果 ──
    sec_result = ttk.LabelFrame(body, text="④ 结果")
    sec_result.pack(fill="x", padx=2, pady=4)
    row_result = ttk.Frame(sec_result)
    row_result.pack(fill="x", pady=(2, 2))
    add_button(row_result, "打开结果文件夹", "open_folder")
    add_button(row_result, "历史记录", "results")
    gray(sec_result, "表格存成 CSV，点「打开结果文件夹」直接看")

    # ── 更多（默认收起：导出 / 关闭后台浏览器 / 视觉抓取）──
    more_wrap = ttk.Frame(body)
    more_wrap.pack(fill="x", padx=2, pady=(2, 2))
    more = ttk.Frame(more_wrap)

    def toggle_more() -> None:
        if more.winfo_ismapped():
            more.pack_forget()
            more_btn.config(text="▸ 更多")
        else:
            more.pack(fill="x")
            more_btn.config(text="▾ 更多")

    more_btn = ttk.Button(more_wrap, text="▸ 更多", command=toggle_more)
    more_btn.pack(side="left", padx=(2, 8))
    add_button(more, "导出登录信息", "export")
    add_button(more, "关闭后台浏览器", "shutdown")
    add_button(more, "视觉抓取", "vision")

    # ── 状态栏（错误红字）+ 日志窗（6 行高，可拖大）──
    bar = ttk.Frame(app.root)
    bar.pack(fill="x", padx=8)
    app.status_var = tk.StringVar(value="空闲")
    app.status_label = ttk.Label(bar, textvariable=app.status_var,
                                 anchor="w")
    app.status_label.pack(side="left", fill="x", expand=True)
    app.cancel_btn = ttk.Button(bar, text="停止", command=app._cancel,
                                state="disabled")
    app.cancel_btn.pack(side="right")

    app.log = scrolledtext.ScrolledText(app.root, height=6,
                                        state="disabled", wrap="word")
    app.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))
