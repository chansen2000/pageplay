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
