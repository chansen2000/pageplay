# pageplay

人登录一次，AI 拿 cookie 或驱动浏览器 —— 通用网站会话工具。

## 工作模型（T10 常驻会话）

浏览器窗口**人开人关、常驻不关**，自动化在同一活窗口里跑：

1. 命令（open/login/pick/run/doctor）自动附着常驻浏览器；没有就起一个
   脱离的守护进程（调试端口和 PID 记在 `session.json`），命令结束只关
   自己开的页，浏览器保持常驻。
2. 遇风控/验证码 → 终端提示"请在浏览器窗口完成登录/验证"，人在窗口里
   操作，通过后自动化**自动续跑**（等人至多 120 秒，重试上限 2 次）。
3. 所有站共享一个浏览器档案 `browser-profile/`（cookie 本就按域隔离）；
   关掉窗口（有头）或 `pageplay shutdown`（无头）= 会话结束。
4. 登录态两份保管：`browser-profile/` 真身 + `state.json` cookie 快照。
5. 定时任务等无人值守场景：`run --headless` 起无头守护跑，收尾用
   `pageplay shutdown` 关掉；无头时遇风控无法人工协助，会明确提示。

## 安装

```bash
pip install -e ".[dev]"
python -m playwright install chromium
```

## 命令

### login：人工登录并保存会话

```bash
pageplay login taobao                    # 内置预设
pageplay login www.taobao.com            # 直接贴网址/域名也行
pageplay login mysite --url https://demo.example.com/login   # 自定义站点
pageplay login taobao --timeout 600      # 调整等待人工登录的超时秒数
```

附着有头窗口打开登录页，你在窗口里完成登录，检测到登录态标记后自动保存。
登录态仍有效时不折腾人，直接提示"无需重复登录"。陌生站（不在内置表）走
回车档：登录完成后回终端按回车保存。

### list / doctor / export

```bash
pageplay list                             # 已保存站点（含快照时间）+ 内置预设
pageplay doctor taobao                    # 验活：有效刷新快照，过期提示重登
pageplay export taobao                    # 导出 cookie（打印）
pageplay export taobao --out cookies.json # 写文件（权限 0600）
```

doctor 附着活窗验活（冷检查 cookie，冷败再开落地页等自动续登）；无活浏览器
时自动起无头守护查完收页（浏览器不关）。export 只读快照，精简 JSON
（name/value/domain/expires），供 curl_cffi 等直用。

### open：打开常驻浏览器窗口

```bash
pageplay open taobao     # 附着/起有头窗口并停在站点首页，命令即返
```

窗口人开人关：保持打开，后续命令自动附到同一窗口；关闭直接关窗或 shutdown。

### forget：删除登录态

```bash
pageplay forget taobao   # 删快照 + meta + recipes
```

### pick：框选表格/元素，确认即得

```bash
pageplay pick taobao --name daily-orders   # 站点名/网址均可，--name 可省
```

有头窗口里 hover 高亮（点单元格自动认整表），↑ 扩选 / ↓ 收回，点击锁定，
表格出列勾选条、普通元素选下载。**连续框选**：确认一条当场执行后继续等
下一条（关窗/空闲超时/Ctrl-C 结束）；第 1 条用 --name，第 2 条起自动
`<名>-2`、`<名>-3`。每条确认即存 recipe + 当场执行 + 落账；会话结束打印
摘要（收了几条、名字列表、产物目录）。

### run：重放 recipe 拿产物

```bash
pageplay run daily-orders --out DIR      # 默认附着有头活窗
pageplay run daily-orders --headless     # 定时任务：无头守护跑
```

产物默认 `~/Downloads/pageplay/<recipe名>/`（表 CSV+JSON 双份、下载存原始
文件名）。被弹回登录页不直接停：等人过验证后自动续跑；选择器等不到提示
重新 pick。结果无论成败落账。

### recipes / results / shutdown

```bash
pageplay recipes                  # 列 recipe；后跟站点名只看某站
pageplay results                  # 执行账本回看（✓✗ 与产物路径，新在前）
pageplay results daily-orders --limit 5
pageplay shutdown                 # 关闭常驻浏览器守护（定时场景收尾）
```

## 存储位置与 PAGEPLAY_HOME

```
~/.pageplay/
├── browser-profile/    # 共享浏览器档案（所有站，登录态真身）
├── session.json        # 常驻守护：调试端口 + PID + headless
├── runs.jsonl          # 执行账本：pick 确认即执行与 run 的每次记录
└── sites/<site>/
    ├── state.json      # cookie 快照（0600）
    ├── meta.json       # 站点名/登录URL/保存时间/检测标记
    └── recipes/        # pick 存的 recipe（json）+ 封面（png）
```

环境变量 `PAGEPLAY_HOME` 可把根目录指到任意位置：
`PAGEPLAY_HOME=/path/to/home pageplay list`

## 安全边界

- 不碰账密：登录只发生在你亲手操作的浏览器窗口里，工具只读 cookie。
- 不破解验证码：滑块/扫码/2FA 一律由人过，工具只等待并自动续跑。
- 档案只在本机：快照与浏览器档案都在本机目录，不上传任何服务器。
- 快照文件权限 0600，仅当前用户可读。

设计文档：`~/Downloads/project设计架构/pageplay/2026-09-23-pageplay-设计.md`。
