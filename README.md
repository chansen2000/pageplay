# pageplay

人登录一次，AI 拿 cookie 或驱动浏览器 —— 通用网站会话工具。

## 工作模型

1. 你在受控浏览器里登录一次（滑块/扫码/2FA 自己过），pageplay 检测到登录态后收割
   cookie 存档 —— 账密永远不经过工具。
2. 登录态两份保管：`browser-profile/` 真身（浏览器自管续期）+ `state.json` cookie 快照。
3. 两种消费方式：AI 拿快照 cookie 配 curl_cffi 走 HTTP 直调；或 `pageplay open` 起同一
   档案的真浏览器做渲染页浏览/下载。

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

弹出浏览器打开登录页，你在窗口里完成登录，pageplay 检测到登录态标记后自动保存并退出。

直接贴网址/域名时两档行为：贴的站命中内置预设（如 `www.taobao.com` 对上
taobao）→ 打开贴的页面，自动检测登录态后保存；陌生站（如
`pageplay login www.newsite.com`）→ 打开贴的页面，你登录完成后回到终端
按回车，pageplay 收取该域 cookie 保存。

### list：查看已登记站点

```bash
pageplay list
```

列出已保存站点（含快照时间）与全部内置预设，未登录的标"未登录"。

### doctor：检查登录态是否有效

```bash
pageplay doctor taobao
```

headless 起档案验活：有效则刷新快照；过期则提示重新 login。

### export：导出 cookie 给脚本消费

```bash
pageplay export taobao                    # 打印到终端
pageplay export taobao --out cookies.json # 写入文件（权限 0600）
pageplay export taobao --out -            # 同打印到终端
```

输出精简 JSON（name/value/domain/expires），供 curl_cffi 等抓取脚本直接消费。

### open：打开带登录态的浏览器

```bash
pageplay open taobao
```

以持久档案打开 headful 浏览器，可交给 AI 用 Playwright 驱动；Ctrl-C 结束。

### forget：删除登录态

```bash
pageplay forget taobao
```

删除该站点的浏览器档案 + 快照 + meta，彻底忘记登录态。

### pick：框选表格/元素，存成可重放的 recipe（v0.2）

```bash
pageplay pick taobao --name daily-orders   # 站点名/网址均可，--name 可省
```

浏览器打开页面：hover 高亮（点单元格自动认整表），↑ 扩选 / ↓ 收回，点击锁定后
表格出列勾选条（默认全勾）、普通元素选下载文件；确认后截封面存 recipe，Esc 取消。

### run：headless 重放 recipe 拿产物（v0.2）

```bash
pageplay run daily-orders --show --out DIR   # 默认 headless；--show 有头看过程
```

产物默认在 `~/Downloads/pageplay/<recipe名>/`：抓表 CSV+JSON 双份、下载存原始
文件；选择器等不到会提示"页面结构可能变了"，重新 pick 即可。

### recipes：列出已有 recipe（v0.2）

```bash
pageplay recipes               # 全部站点；后跟站点名只看某站
```

## 存储位置与 PAGEPLAY_HOME

默认存储在 `~/.pageplay/sites/<站点名>/`：

```
~/.pageplay/sites/<site>/
├── browser-profile/   # 浏览器档案（登录态真身）
├── state.json         # cookie 快照（0600）
├── meta.json          # 站点名/登录URL/保存时间/检测标记
└── recipes/           # pick 存的 recipe（json）+ 封面图（png）
```

环境变量 `PAGEPLAY_HOME` 可把根目录指到任意位置：

```bash
PAGEPLAY_HOME=/path/to/home pageplay list
```

## 安全边界

- 不碰账密：登录只发生在你亲手操作的浏览器窗口里，工具只读 cookie。
- 不破解验证码：滑块/扫码/2FA 一律由人过，工具只等待。
- 档案只在本机：快照与浏览器档案都存在本机目录，不上传任何服务器。
- 快照文件权限 0600，仅当前用户可读。

设计文档：`~/Downloads/project设计架构/pageplay/2026-09-23-pageplay-设计.md`。
