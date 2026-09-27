"""pytest 共享夹具：本地假登录站 + PAGEPLAY_HOME 隔离（绝不碰真实 ~/.pageplay）。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_LOGIN_HTML = """<html><body>
<h1>演示站登录</h1>
<form action="/login" method="post">
  <input name="username" placeholder="用户名">
  <input name="password" type="password" placeholder="密码">
  <button type="submit">登录</button>
</form>
</body></html>"""


class _FakeSiteHandler(BaseHTTPRequestHandler):
    """最小假登录站：GET /login 表单页；POST /login 发 cookie 并 302 /home。"""

    def do_GET(self) -> None:
        if self.path.startswith("/login"):
            self._send_html(200, _LOGIN_HTML)
        elif self.path.startswith("/home"):
            self._send_html(200, "ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 消费表单请求体（内容不校验）
        self.send_response(302)
        self.send_header("Location", "/home")
        self.send_header("Set-Cookie", "sessionid=test123; Path=/")
        self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假站访问日志刷进测试输出


@pytest.fixture
def fake_site():
    """起本地假登录站，yield base_url（如 http://127.0.0.1:PORT），测完关停。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PAGEPLAY_HOME 指到临时目录，返回根路径（sites 根 = <root>/sites）。"""
    root = tmp_path / "pageplay-home"
    monkeypatch.setenv("PAGEPLAY_HOME", str(root))
    return root


@pytest.fixture(autouse=True)
def _isolate_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底隔离：任何测试（含不关心 PAGEPLAY_HOME 的旧测试）都不碰真实家目录。"""
    monkeypatch.setenv("PAGEPLAY_HOME", str(tmp_path / "pageplay-home-autouse"))


@pytest.fixture
def _daemon_cleanup():
    """兜底收尾：测试结束后关掉常驻守护，并收掉测试进程内的 playwright 驱动。

    生产里 CLI 进程退出即带走驱动；测试进程同线程复用会撞上仍活的
    asyncio loop（"Sync API inside asyncio loop"），必须显式收掉。
    守护浏览器本身已脱离父进程，shutdown_browser 走 pid 终止。
    """
    yield
    from pageplay import session as _session

    _session.shutdown_browser()
    driver, _session._PW = _session._PW, None
    if driver is not None:
        try:
            driver.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# T7b：表格/下载假站（actions 执行器测试用，T7d run 流程同样消费）
# ---------------------------------------------------------------------------

_TABLE_HTML = """<html><body>
<h1>数据导出</h1>
<table id="data">
  <thead>
    <tr><th>名称</th><th>价格</th><th>库存</th><th>链接</th></tr>
  </thead>
  <tbody>
    <tr><td>商品1</td><td>10</td><td>100</td><td><a href="/item/1">商品1</a></td></tr>
    <tr><td>商品2</td><td>20</td><td>90</td><td><a href="/item/2">商品2</a></td></tr>
    <tr><td>商品3</td><td>30</td><td>80</td><td><a href="/item/3">商品3</a></td></tr>
    <tr><td>商品4</td><td>40</td><td>70</td><td><a href="/item/4">商品4</a></td></tr>
    <tr><td>商品5</td><td>50</td><td>60</td><td><a href="/item/5">商品5</a></td></tr>
  </tbody>
</table>
</body></html>"""

_DOWNLOAD_HTML = """<html><body>
<h1>导出中心</h1>
<p><a id="dl" href="/file.xlsx">导出</a></p>
</body></html>"""


class _TableSiteHandler(BaseHTTPRequestHandler):
    """表格假站：GET /table 数据表页；GET /download 下载入口页；
    GET /file.xlsx 附件体（Content-Disposition 指名 report.xlsx）。"""

    def do_GET(self) -> None:
        if self.path.startswith("/table"):
            self._send_html(200, _TABLE_HTML)
        elif self.path.startswith("/download"):
            self._send_html(200, _DOWNLOAD_HTML)
        elif self.path.startswith("/file.xlsx"):
            body = b"fake-xlsx-content"
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header(
                "Content-Disposition", 'attachment; filename="report.xlsx"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假站访问日志刷进测试输出


@pytest.fixture
def table_site():
    """起本地表格假站（端口 0），yield base_url 字符串，测完关停。

    路由：GET /table → id="data" 表格页；GET /download → 导出链接页；
    GET /file.xlsx → 附件字节体。风格与 fake_site 一致。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TableSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# T8：跨页导航假站（picker 框选层跨页存活测试用）
# ---------------------------------------------------------------------------

_NAV_START_HTML = """<html><body>
<h1>导航起点</h1>
<a id="go" href="/table">去表格页</a>
</body></html>"""


class _NavSiteHandler(BaseHTTPRequestHandler):
    """跨页假站：GET /start 含指向 /table 的链接；GET /table 复用表格页。"""

    def do_GET(self) -> None:
        if self.path.startswith("/start"):
            self._send_html(200, _NAV_START_HTML)
        elif self.path.startswith("/table"):
            self._send_html(200, _TABLE_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假站访问日志刷进测试输出


@pytest.fixture
def nav_site():
    """起本地跨页导航假站（端口 0），yield base_url 字符串，测完关停。

    路由：GET /start → 含 <a id="go" href="/table"> 的起点页；
    GET /table → 与 table_site 同款 4 列表格页（id="data"）。
    风格与 fake_site / table_site 一致。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NavSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# T10：风控协作假站（登录反弹 → 人过验证 → 自动续跑）
# ---------------------------------------------------------------------------

# 时序来源（防 flaky 约定，2026-09-25 全量偶发失败整改）：跳 /pass 的延时
# 锚定在 load 事件之后而非脚本求值时刻——page.goto 默认等 load 才返回，
# "load 后才起表"保证 goto 的在途等待永远先于跳转完成，满载下也不会出现
# 定时器打断在途导航；1.5s 余量给紧随 goto 的反弹判定/风控扫描（微秒级
# 读）让路。cookie 落袋由产品侧 ensure_logged_in 轮询（2s 间隔、120s 预算）
# 等到，测试不赌任何固定时间点。
_COLLAB_LOGIN_HTML = """<html><body>
<h1>安全验证</h1>
<p>滑块验证：请拖动滑块完成拼图</p>
<script>window.addEventListener("load", function () {{
  setTimeout(function () {{ location.href = "{pass_url}"; }}, 1500);
}});</script>
</body></html>"""

_COLLAB_HOME_HTML = """<html><body>
<h1>站点首页</h1>
<script>window.addEventListener("load", function () {{
  setTimeout(function () {{ location.href = "{pass_url}"; }}, 1500);
}});</script>
</body></html>"""

_COLLAB_TABLE_HTML = """<html><body>
<h1>订单列表</h1>
<table id="data">
  <thead><tr><th>名称</th><th>价格</th></tr></thead>
  <tbody>
    <tr><td>商品1</td><td>10</td></tr>
    <tr><td>商品2</td><td>20</td></tr>
  </tbody>
</table>
</body></html>"""


class _CollabSiteHandler(BaseHTTPRequestHandler):
    """风控协作假站（双主机方案）。

    中性主机 = 127.0.0.1:<port>（本 fixture 的 base_url）：/table 未带
    cookie 时 302 到 http://login.localhost:<port>/login——落点 host 含
    "login."，run 的登录反弹判定命中。滑块页 600ms 后跳 /pass（绝对地址
    回中性主机），/pass 回 Set-Cookie sessionid——"人过验证"后登录标记
    出现在中性域，与真人滑块通过后 cookie 落袋同一可观测量（sync API
    禁跨线程，不另起线程碰 playwright）。
    """

    def _port(self) -> int:
        return self.server.server_address[1]

    def do_GET(self) -> None:
        port = self._port()
        neutral = f"http://127.0.0.1:{port}"
        logged_in = "sessionid=" in (self.headers.get("Cookie") or "")
        if self.path.startswith("/table"):
            if logged_in:
                self._send_html(200, _COLLAB_TABLE_HTML)
            else:
                self.send_response(302)
                self.send_header("Location", f"http://login.localhost:{port}/login")
                self.end_headers()
        elif self.path.startswith("/login"):
            self._send_html(200, _COLLAB_LOGIN_HTML.format(pass_url=neutral + "/pass"))
        elif self.path.startswith("/home"):
            self._send_html(200, _COLLAB_HOME_HTML.format(pass_url=neutral + "/pass"))
        elif self.path.startswith("/pass"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Set-Cookie", "sessionid=collab123; Path=/")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假站访问日志刷进测试输出


@pytest.fixture
def collab_site():
    """起风控协作假站（端口 0），yield 中性 base_url 字符串，测完关停。

    路由（中性主机视角）：GET /table 带 sessionid → 表格页（id="data"），
    未带 → 302 http://login.localhost:<port>/login（反弹落点）；GET /login
    → 滑块页（延时跳中性 /pass 种 cookie）；GET /home → 落地页（同）；
    GET /pass → Set-Cookie sessionid。风格与 fake_site 等一致。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollabSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# T11b：流程录制假站（recorder 录制层测试用）
# ---------------------------------------------------------------------------

_FLOW_START_HTML = """<html><body>
<h1>录制起点</h1>
<a id="to-list" href="/list">去列表页</a>
<a id="to-other" href="/other">外部链接</a>
<table id="t1">
  <thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>
  <tbody>
    <tr><td>a1</td><td>b1</td><td>c1</td></tr>
    <tr><td>a2</td><td>b2</td><td>c2</td></tr>
    <tr><td>a3</td><td>b3</td><td>c3</td></tr>
  </tbody>
</table>
</body></html>"""

_FLOW_LIST_HTML = """<html><body>
<h1>列表页</h1>
<table id="t2">
  <thead><tr><th>名称</th><th>价格</th></tr></thead>
  <tbody>
    <tr><td>苹果</td><td>5.5</td></tr>
    <tr><td>香蕉</td><td>3.2</td></tr>
  </tbody>
</table>
<a id="back" href="/start">回起点</a>
</body></html>"""

_FLOW_OTHER_HTML = """<html><body>
<h1>外部落点</h1>
</body></html>"""


class _FlowSiteHandler(BaseHTTPRequestHandler):
    """录制假站：GET /start 起点页（内链 /list + 外链样式 /other +
    id="t1" 3 行 3 列小表）；GET /list 列表页（id="t2" 两列表 + 回程
    /start 链接）；GET /other 落点页。能同时演"点击跳转 + P 框选"。
    """

    def do_GET(self) -> None:
        if self.path.startswith("/start"):
            self._send_html(200, _FLOW_START_HTML)
        elif self.path.startswith("/list"):
            self._send_html(200, _FLOW_LIST_HTML)
        elif self.path.startswith("/other"):
            self._send_html(200, _FLOW_OTHER_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假站访问日志刷进测试输出


@pytest.fixture
def flow_site():
    """起流程录制假站（端口 0），yield base_url 字符串，测完关停。

    路由：GET /start → 起点页（<a id="to-list" href="/list">、
    <a id="to-other" href="/other">、id="t1" 3 行 3 列表）；
    GET /list → 列表页（id="t2" 两列表、<a id="back" href="/start">）；
    GET /other → 落点页。风格与 fake_site / nav_site 一致。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlowSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# v0.7-A：卡片列表假站（picker 卡片识别 / actions extract_cards / runner 用）
# ---------------------------------------------------------------------------

# 6 张同签名 div 卡片（class="card"，兄弟同级在 .list 容器下），每张含
# 订单号/标题/价格/状态四个文本节点（嵌套 div/span，非 table）。
# 首卡标题故意 9 字：验 pickFields 的 label 截前 8 字。
_CARDS_HTML = """<html><body>
<h1>订单列表</h1>
<div class="list">
  <div class="card"><div class="order-no">TB9001</div><div class="info"><span class="title">无线蓝牙鼠标静音版</span><span class="price">99.0</span><span class="state">已发货</span></div></div>
  <div class="card"><div class="order-no">TB9002</div><div class="info"><span class="title">机械键盘青轴</span><span class="price">299.0</span><span class="state">已发货</span></div></div>
  <div class="card"><div class="order-no">TB9003</div><div class="info"><span class="title">USB-C 扩展坞</span><span class="price">159.0</span><span class="state">待发货</span></div></div>
  <div class="card"><div class="order-no">TB9004</div><div class="info"><span class="title">27 寸显示器</span><span class="price">899.0</span><span class="state">待发货</span></div></div>
  <div class="card"><div class="order-no">TB9005</div><div class="info"><span class="title">笔记本支架</span><span class="price">79.0</span><span class="state">已签收</span></div></div>
  <div class="card"><div class="order-no">TB9006</div><div class="info"><span class="title">降噪耳机</span><span class="price">499.0</span><span class="state">已签收</span></div></div>
</div>
</body></html>"""


class _CardSiteHandler(BaseHTTPRequestHandler):
    """卡片列表假站：GET /cards → 6 张同签名 div 卡片页（无重复组对照
    页不另设，复用 table_site 的 /table 或页面里的 h1 等非列表元素）。"""

    def do_GET(self) -> None:
        if self.path.startswith("/cards"):
            self._send_html(200, _CARDS_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假站访问日志刷进测试输出


@pytest.fixture
def card_site():
    """起卡片列表假站（端口 0），yield base_url 字符串，测完关停。

    路由：GET /cards → 6 张同签名 div.card 卡片（订单号/标题/价格/状态
    四个文本节点，嵌套 div/span，非 table）。风格与 table_site 一致。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CardSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# v0.10：淘宝形态列表假站（listscan 引擎 / picker list 面板 / runner 用）
# ---------------------------------------------------------------------------

# 6 单 10 件（每单 1/2/3/1/1/2 件），复刻真机买家订单页形态（设计 §2/§8）：
# - 全部语义 class 带 -- 哈希后缀；表头 orderHeader 与订单兄弟同层（不同 sem）
# - 价格 4 段 span（￥/650/./00）同行相邻；"订单号: " 与号码同宿主（§2 实测形态）
# - 首单内嵌 extBlank 推荐区（"常买常逛-推荐商品位"）；旺旺在线 display:none
# - 商品标题宿主在 a[href] 内
_LIST_HTML = """<html><head><style>
.order{margin:12px 0;padding:6px;border:1px solid #eee}
.hd{display:block;font-weight:bold}
.tipHidden{display:none}
[class*='itemInfo'] a,[class*='itemSku'],[class*='qty']{display:block}
</style></head><body>
<div class="tradeContent--Wrap0">
  <div class="orderHeader--Hd00">订单信息 | 商品 | 单价 | 数量 | 实付款 | 操作</div>

  <div class="tradeOrder--A100"><div class="shopInfo--B100">
    <span class="orderTime--C100">2026-09-21</span>
    <span class="orderId--D100">订单号: 8801</span>
    <a class="shopName--E100" href="/shop/minguang">敏光企业店</a>
    <span class="orderStatus--F100">卖家已发货</span>
    <span class="payTotal--G100"><span class="payLabel--H100">实付款</span><span class="pSymbol--I100">￥</span><span class="pInt--J100">650</span><span class="pDot--K100">.</span><span class="pDec--L100">00</span></span>
    <span class="tipHidden--M100">旺旺在线</span>
  </div>
  <div class="itemList--N100">
    <div class="itemInfo--O100"><a class="itemTitle--P100" href="/item/lsspd">LSSPD-1.2 光电探测器</a><span class="itemSku--Q100">LSSPD-1.2-3P 3管脚</span><span class="itemPrice--R100"><span class="pSymbol--I100">￥</span><span class="pInt--J100">50</span><span class="pDot--K100">.</span><span class="pDec--L100">00</span></span><span class="qty--S100">x13</span></div>
  </div>
  <div class="extBlank--T100"><div class="recText--U100">常买常逛-推荐商品位</div></div>
  </div>

  <div class="tradeOrder--A200"><div class="shopInfo--B200">
    <span class="orderTime--C200">2026-09-17</span>
    <span class="orderId--D200">订单号: 8802</span>
    <a class="shopName--E200" href="/shop/baobao">宝宝家居</a>
    <span class="orderStatus--F200">卖家已发货</span>
    <span class="payTotal--G200"><span class="payLabel--H200">实付款</span><span class="pSymbol--I200">￥</span><span class="pInt--J200">144</span><span class="pDot--K200">.</span><span class="pDec--L200">40</span></span>
  </div>
  <div class="itemList--N200">
    <div class="itemInfo--O200"><a class="itemTitle--P200" href="/item/gui30">收纳柜 30CM版</a><span class="itemSku--Q200">30CM1大3小【白色】</span><span class="itemPrice--R200"><span class="pSymbol--I200">￥</span><span class="pInt--J200">79</span><span class="pDot--K200">.</span><span class="pDec--L200">00</span></span><span class="qty--S200">x1</span></div>
    <div class="itemInfo--O200"><a class="itemTitle--P200" href="/item/gui20">收纳柜 20CM版</a><span class="itemSku--Q200">20CM1大3小【白色】</span><span class="itemPrice--R200"><span class="pSymbol--I200">￥</span><span class="pInt--J200">65</span><span class="pDot--K200">.</span><span class="pDec--L200">40</span></span><span class="qty--S200">x1</span></div>
  </div></div>

  <div class="tradeOrder--A300"><div class="shopInfo--B300">
    <span class="orderTime--C300">2026-09-15</span>
    <span class="orderId--D300">订单号: 8803</span>
    <a class="shopName--E300" href="/shop/dianyuan">电源之家</a>
    <span class="orderStatus--F300">已签收</span>
    <span class="payTotal--G300"><span class="payLabel--H300">实付款</span><span class="pSymbol--I300">￥</span><span class="pInt--J300">300</span><span class="pDot--K300">.</span><span class="pDec--L300">00</span></span>
  </div>
  <div class="itemList--N300">
    <div class="itemInfo--O300"><a class="itemTitle--P300" href="/item/dy1">12V 电源 A</a><span class="itemSku--Q300">12V 2A</span><span class="itemPrice--R300"><span class="pSymbol--I300">￥</span><span class="pInt--J300">100</span><span class="pDot--K300">.</span><span class="pDec--L300">00</span></span><span class="qty--S300">x1</span></div>
    <div class="itemInfo--O300"><a class="itemTitle--P300" href="/item/dy2">12V 电源 B</a><span class="itemSku--Q300">12V 5A</span><span class="itemPrice--R300"><span class="pSymbol--I300">￥</span><span class="pInt--J300">120</span><span class="pDot--K300">.</span><span class="pDec--L300">00</span></span><span class="qty--S300">x1</span></div>
    <div class="itemInfo--O300"><a class="itemTitle--P300" href="/item/dy3">12V 电源 C</a><span class="itemSku--Q300">24V 2A</span><span class="itemPrice--R300"><span class="pSymbol--I300">￥</span><span class="pInt--J300">80</span><span class="pDot--K300">.</span><span class="pDec--L300">00</span></span><span class="qty--S300">x1</span></div>
  </div></div>

  <div class="tradeOrder--A400"><div class="shopInfo--B400">
    <span class="orderTime--C400">2026-09-12</span>
    <span class="orderId--D400">订单号: 8804</span>
    <a class="shopName--E400" href="/shop/xiancai">线材铺</a>
    <span class="orderStatus--F400">已签收</span>
    <span class="payTotal--G400"><span class="payLabel--H400">实付款</span><span class="pSymbol--I400">￥</span><span class="pInt--J400">25</span><span class="pDot--K400">.</span><span class="pDec--L400">50</span></span>
  </div>
  <div class="itemList--N400">
    <div class="itemInfo--O400"><a class="itemTitle--P400" href="/item/cable">USB 线 1 米</a><span class="itemSku--Q400">Type-C</span><span class="itemPrice--R400"><span class="pSymbol--I400">￥</span><span class="pInt--J400">25</span><span class="pDot--K400">.</span><span class="pDec--L400">50</span></span><span class="qty--S400">x1</span></div>
  </div></div>

  <div class="tradeOrder--A500"><div class="shopInfo--B500">
    <span class="orderTime--C500">2026-09-10</span>
    <span class="orderId--D500">订单号: 8805</span>
    <a class="shopName--E500" href="/shop/tiepian">贴片世界</a>
    <span class="orderStatus--F500">卖家已发货</span>
    <span class="payTotal--G500"><span class="payLabel--H500">实付款</span><span class="pSymbol--I500">￥</span><span class="pInt--J500">9</span><span class="pDot--K500">.</span><span class="pDec--L500">90</span></span>
  </div>
  <div class="itemList--N500">
    <div class="itemInfo--O500"><a class="itemTitle--P500" href="/item/chip0805">贴片电阻 0805</a><span class="itemSku--Q500">10K 1%</span><span class="itemPrice--R500"><span class="pSymbol--I500">￥</span><span class="pInt--J500">9</span><span class="pDot--K500">.</span><span class="pDec--L500">90</span></span><span class="qty--S500">x1</span></div>
  </div></div>

  <div class="tradeOrder--A600"><div class="shopInfo--B600">
    <span class="orderTime--C600">2026-09-08</span>
    <span class="orderId--D600">订单号: 8806</span>
    <a class="shopName--E600" href="/shop/wanbiao">万表阁</a>
    <span class="orderStatus--F600">待发货</span>
    <span class="payTotal--G600"><span class="payLabel--H600">实付款</span><span class="pSymbol--I600">￥</span><span class="pInt--J600">520</span><span class="pDot--K600">.</span><span class="pDec--L600">00</span></span>
  </div>
  <div class="itemList--N600">
    <div class="itemInfo--O600"><a class="itemTitle--P600" href="/item/watchA">机械表 A 款</a><span class="itemSku--Q600">黑盘钢带</span><span class="itemPrice--R600"><span class="pSymbol--I600">￥</span><span class="pInt--J600">300</span><span class="pDot--K600">.</span><span class="pDec--L600">00</span></span><span class="qty--S600">x1</span></div>
    <div class="itemInfo--O600"><a class="itemTitle--P600" href="/item/watchB">机械表 B 款</a><span class="itemSku--Q600">白盘皮带</span><span class="itemPrice--R600"><span class="pSymbol--I600">￥</span><span class="pInt--J600">220</span><span class="pDot--K600">.</span><span class="pDec--L600">00</span></span><span class="qty--S600">x1</span></div>
  </div></div>
</div>
</body></html>"""


class _ListSiteHandler(BaseHTTPRequestHandler):
    """淘宝形态列表假站：GET /list → 6 单 10 件订单列表页。"""

    def do_GET(self) -> None:
        if self.path.startswith("/list"):
            self._send_html(200, _LIST_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


@pytest.fixture
def list_site():
    """起淘宝形态列表假站（端口 0），yield base_url，测完关停。

    路由：GET /list → 6 单 10 件订单页（哈希 class、表头兄弟、价格 4 段
    span、首单内嵌推荐区、display:none 文本、a[href] 标题，§2/§8 形态）。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ListSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# v0.10：文件假站（download_links：3 个像文件链接 + 1 个普通链接 + 1 个去重）
# ---------------------------------------------------------------------------

_FILES = {"a.pdf": b"%PDF-fake-a", "b.xlsx": b"XLSX-fake-b", "c.bin": b"BIN-fake-c"}
_LINKS_PAGE = """<html><body><div id="box">
  <a id="f1" href="/files/a.pdf">报表A</a>
  <a id="f2" href="/files/b.xlsx">报表B</a>
  <a id="f3" href="/files/c.bin" download>打包C</a>
  <a id="f4" href="/home">普通链接（不像文件）</a>
  <a id="f5" href="/files/a.pdf">报表A重复（去重用）</a>
</div></body></html>"""


class _FileSiteHandler(BaseHTTPRequestHandler):
    """文件假站：/links 链接页 + /files/* 字节（a.pdf 带 Content-Disposition）。"""

    def do_GET(self) -> None:
        if self.path.startswith("/links"):
            self._send_html(200, _LINKS_PAGE)
        elif self.path.startswith("/files/"):
            name = self.path.rsplit("/", 1)[-1]
            data = _FILES.get(name)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            if name == "a.pdf":  # 文件名优先级：Content-Disposition > URL 末段
                self.send_header("Content-Disposition",
                                 'attachment; filename="report-a.pdf"')
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/home"):
            self._send_html(200, "<html><body>普通页</body></html>")
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


@pytest.fixture
def file_site():
    """起文件假站（端口 0）：GET /links 链接页、/files/* 字节、/home 普通页。

    a.pdf 落 Content-Disposition（文件名优先级用例）；b.xlsx 靠扩展名、
    c.bin 靠 download 属性凑「像文件」三通道；/home 不像文件不下载。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FileSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
