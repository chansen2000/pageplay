"""T11c 整链重放 runner 测试：真 chromium + 本文件自建 threading 假站。

自建不碰 conftest（T11b 并行独占追加权）。假站沿用 T10 双主机方案：
中性主机 127.0.0.1（首页/列表/深层/受保护页/风控页），受保护页无
cookie 时 302 到 http://login.localhost:<port>/login——落点 host 含
"login."，反弹判定命中（Chromium 把 *.localhost 解析到回环，T10 已实测）。

浏览器：真无头守护（session.ensure_browser，PAGEPLAY_HOME 指临时目录，
收尾关守护+收驱动，风格同 test_daemon）。"人过了登录"的模拟：按
runner 的 import 位置 monkeypatch pageplay.runner.ensure_logged_in——
向守护上下文种 sessionid cookie 后返回 True（与真人过验证后 cookie
落袋同一可观测量）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from pageplay import runner, session  # noqa: E402
from pageplay.guard import RiskTriggered  # noqa: E402


# ---------------------------------------------------------------------------
# 自建假站（双主机：中性 127.0.0.1 + 反弹落点 login.localhost）
# ---------------------------------------------------------------------------

_HOME_HTML = """<html><body>
<h1>流程起点</h1>
<a id="to-list" href="/list">去看列表</a>
</body></html>"""

_LIST_HTML = """<html><body>
<h1>数据列表</h1>
<table id="t1">
  <thead><tr><th>名称</th><th>价格</th><th>库存</th></tr></thead>
  <tbody>
    <tr><td>商品1</td><td>10</td><td>100</td></tr>
    <tr><td>商品2</td><td>20</td><td>90</td></tr>
    <tr><td>商品3</td><td>30</td><td>80</td></tr>
  </tbody>
</table>
<a id="to-deep" href="/deep">去导出页</a>
</body></html>"""

_DEEP_HTML = """<html><body>
<h1>导出中心</h1>
<a id="dl" href="/file">下载报表</a>
</body></html>"""

_PRIVATE_HTML = """<html><body>
<h1>私有页</h1>
<table id="pt"><tr><th>名</th></tr><tr><td>a</td></tr></table>
</body></html>"""


class _FlowSiteHandler(BaseHTTPRequestHandler):
    """整链重放假站：列表/下载/受保护重定向/风控页（一服多路由）。

    /private 无 sessionid cookie → 302 http://login.localhost:<port>/login
    （反弹落点）；/login 塞"滑块"文案（只在反弹路径可达，协作判定先于
    风控检查，不会误触真风控）；/risk 在中性主机塞"滑块"（真风控用）。
    """

    def _port(self) -> int:
        return self.server.server_address[1]

    def do_GET(self) -> None:
        port = self._port()
        logged_in = "sessionid=" in (self.headers.get("Cookie") or "")
        if self.path.startswith("/list"):
            self._send_html(200, _LIST_HTML)
        elif self.path.startswith("/deep"):
            self._send_html(200, _DEEP_HTML)
        elif self.path.startswith("/file"):
            body = b"colA,colB\n1,2\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header(
                "Content-Disposition", 'attachment; filename="report.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/private"):
            if logged_in:
                self._send_html(200, _PRIVATE_HTML)
            else:
                self.send_response(302)
                self.send_header(
                    "Location", f"http://login.localhost:{port}/login")
                self.end_headers()
        elif self.path.startswith("/login"):
            self._send_html(200, "<html><body><h1>滑块验证</h1></body></html>")
        elif self.path.startswith("/risk"):
            self._send_html(200, "<html><body><h1>滑块</h1></body></html>")
        else:
            self._send_html(200, _HOME_HTML)  # / 及其余 → 首页

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
    """起整链重放假站（端口 0），yield base_url，测完关停。"""
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


@pytest.fixture
def flow_browser(tmp_path, monkeypatch):
    """真无头守护：内核可用才跑；PAGEPLAY_HOME 隔离；收尾关守护+收驱动。"""
    monkeypatch.setenv("PAGEPLAY_HOME", str(tmp_path / "pageplay-home"))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
            if not (exe and Path(exe).exists()):
                raise RuntimeError(f"内核缺失：{exe}")
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过 runner 链：{exc}")
    browser = session.ensure_browser(headless=True)
    yield browser
    session.shutdown_browser()
    # 生产 CLI 进程退出即带走驱动；测试进程须显式收掉并清单例
    driver, session._PW = session._PW, None
    if driver is not None:
        try:
            driver.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# "人过登录"替身（按 runner 的 import 位置 patch）
# ---------------------------------------------------------------------------

def _patch_human_pass(monkeypatch, base_url: str) -> list[int]:
    """协作即"人过了"：种 sessionid cookie（=真人过验证后落袋）再返 True。"""
    calls: list[int] = []

    def fake_logged_in(browser, site, max_wait=120):
        calls.append(1)
        browser.contexts[0].add_cookies([
            {"name": "sessionid", "value": "human-passed", "url": base_url,
             "expires": -1},
        ])
        return True

    monkeypatch.setattr(runner, "ensure_logged_in", fake_logged_in)
    return calls


def _patch_human_refuses(monkeypatch) -> list[int]:
    calls: list[int] = []

    def fake_logged_in(browser, site, max_wait=120):
        calls.append(1)
        return False

    monkeypatch.setattr(runner, "ensure_logged_in", fake_logged_in)
    return calls


# ---------------------------------------------------------------------------
# 全链 happy path
# ---------------------------------------------------------------------------

def test_run_flow_happy_path_all_steps_ok(flow_browser, flow_site, tmp_path):
    """goto→click→table→click→download 五步全 ok；表产物内容对、下载落盘。"""
    base = flow_site
    out_dir = tmp_path / "out"
    pages_before = len(flow_browser.contexts[0].pages)  # 守护自带 new-tab 页
    flow = {
        "name": "happy",
        "home_url": f"{base}/",
        "steps": [
            {"no": 1, "kind": "goto", "url": f"{base}/"},
            {"no": 2, "kind": "click", "selector": "#to-list"},
            {"no": 3, "kind": "table", "selector": "#t1",
             "columns": ["名称", "价格"]},
            {"no": 4, "kind": "click", "selector": "#to-deep"},
            {"no": 5, "kind": "download", "selector": "#dl"},
        ],
    }
    result = runner.run_flow(flow_browser, flow, out_dir)

    assert result["ok"] is True
    assert result["failed_step"] is None
    assert len(result["results"]) == 5
    assert [r["kind"] for r in result["results"]] == [
        "goto", "click", "table", "click", "download"]
    assert all(r["status"] == "ok" for r in result["results"])

    # table 步产物：stem 用 flow 名 + 步号，CSV+JSON 双份、两列三行
    table_detail = result["results"][2]["detail"]
    assert str(out_dir / "happy-3.csv") in table_detail
    assert str(out_dir / "happy-3.json") in table_detail
    rows = json.loads((out_dir / "happy-3.json").read_text(encoding="utf-8"))
    assert len(rows) == 3
    assert rows[0] == {"名称": "商品1", "价格": "10"}
    assert rows[2]["价格"] == "30"
    csv_text = (out_dir / "happy-3.csv").read_bytes().decode("utf-8-sig")
    assert csv_text.splitlines()[0] == "名称,价格"
    assert csv_text.splitlines()[1] == "商品1,10"

    # download 步产物：Content-Disposition 指名的 report.csv，字节体一致
    download_detail = result["results"][4]["detail"]
    assert str((out_dir / "report.csv").resolve()) in download_detail
    assert (out_dir / "report.csv").read_bytes() == b"colA,colB\n1,2\n"

    # 全程只开一个页、完事关页（关页不关浏览器；守护自带页仍在）
    assert len(flow_browser.contexts[0].pages) == pages_before


def test_run_flow_explicit_stem_names_products(flow_browser, flow_site, tmp_path):
    """stem 显式给定时产物名 = <stem>-<no>（覆盖 stem 优先于 flow 名分支）。"""
    base = flow_site
    flow = {"name": "happy", "steps": [
        {"no": 1, "kind": "goto", "url": f"{base}/list"},
        {"no": 2, "kind": "table", "selector": "#t1", "columns": None},
    ]}
    result = runner.run_flow(flow_browser, flow, tmp_path / "out", stem="custom")
    assert result["ok"] is True
    rows = json.loads(
        (tmp_path / "out" / "custom-2.json").read_text(encoding="utf-8"))
    assert len(rows) == 3 and rows[0]["名称"] == "商品1"


# ---------------------------------------------------------------------------
# 登录反弹协作（受保护路由 302 → login.localhost → 协作 → 重试）
# ---------------------------------------------------------------------------

def test_run_flow_login_bounce_recovers_then_finishes(
        flow_browser, flow_site, tmp_path, monkeypatch):
    """goto 受保护页被弹回登录页 → 协作"人过了"（种 cookie）→ 重试通过
    → 后续步继续；协作调用计数 ≤ MAX_LOGIN_RETRY。"""
    calls = _patch_human_pass(monkeypatch, flow_site)
    base = flow_site
    flow = {
        "name": "priv",
        "home_url": f"{base}/",
        "steps": [
            {"no": 1, "kind": "goto", "url": f"{base}/private"},
            {"no": 2, "kind": "table", "selector": "#pt", "columns": None},
        ],
    }
    result = runner.run_flow(flow_browser, flow, tmp_path / "out")

    assert result["ok"] is True
    assert result["failed_step"] is None
    assert len(calls) <= runner.MAX_LOGIN_RETRY
    assert calls  # 确实进过协作
    rows = json.loads(
        (tmp_path / "out" / "priv-2.json").read_text(encoding="utf-8"))
    assert rows == [{"名": "a"}]


def test_run_flow_login_bounce_collab_fails_reports_human_words(
        flow_browser, flow_site, tmp_path, monkeypatch):
    """协作等人未通过（False）→ ok=False + failed_step + 人话 detail。"""
    calls = _patch_human_refuses(monkeypatch)
    base = flow_site
    flow = {"name": "priv", "home_url": f"{base}/", "steps": [
        {"no": 1, "kind": "goto", "url": f"{base}/private"},
        {"no": 2, "kind": "table", "selector": "#pt", "columns": None},
    ]}
    result = runner.run_flow(flow_browser, flow, tmp_path / "out")

    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert len(calls) == 1  # 等待未通过：照 cli_pick 语义不再重试
    (step_result,) = result["results"]
    assert step_result["status"] == "fail"
    assert "登录态失效" in step_result["detail"]
    assert "未通过" in step_result["detail"]


# ---------------------------------------------------------------------------
# 中途步卡住（顺序语义：后续步不执行）
# ---------------------------------------------------------------------------

def test_run_flow_selector_timeout_stops_later_steps(
        flow_browser, flow_site, tmp_path, monkeypatch):
    """第 2 步选择器等不到 → 该步 fail（人话），第 3 步起未执行。"""
    monkeypatch.setattr(runner, "SELECTOR_TIMEOUT_MS", 400)  # 测试提速
    base = flow_site
    out_dir = tmp_path / "out"
    flow = {"name": "stuck", "steps": [
        {"no": 1, "kind": "goto", "url": f"{base}/"},
        {"no": 2, "kind": "click", "selector": "#nope"},
        {"no": 3, "kind": "table", "selector": "#t1", "columns": None},
        {"no": 4, "kind": "download", "selector": "#dl"},
    ]}
    result = runner.run_flow(flow_browser, flow, out_dir)

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert len(result["results"]) == 2  # 第 3/4 步未执行
    assert result["results"][0]["status"] == "ok"
    stuck = result["results"][1]
    assert stuck["status"] == "fail"
    assert stuck["detail"] == "第 2 步卡住：页面结构可能变了"
    assert not out_dir.exists()  # 产物目录都没建 = 没有任何落盘动作


# ---------------------------------------------------------------------------
# 真风控：非登录页命中 → RiskTriggered 向上抛
# ---------------------------------------------------------------------------

def test_run_flow_risk_hit_propagates(flow_browser, flow_site, tmp_path):
    """中性主机页面塞"滑块"（非登录页）→ RiskTriggered 向上抛；页已关。"""
    base = flow_site
    pages_before = len(flow_browser.contexts[0].pages)  # 守护自带 new-tab 页
    flow = {"name": "risk", "steps": [
        {"no": 1, "kind": "goto", "url": f"{base}/risk"},
        {"no": 2, "kind": "click", "selector": "#to-list"},
    ]}
    with pytest.raises(RiskTriggered, match="滑块"):
        runner.run_flow(flow_browser, flow, tmp_path / "out")
    # 抛出前页已收（finally）：只剩守护自带的页
    assert len(flow_browser.contexts[0].pages) == pages_before
