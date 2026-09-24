"""集成测试：真浏览器（Playwright chromium）跑通 种登录态 → doctor → export → run 全链。

前置：.venv/bin/python -m playwright install chromium
内核/驱动起不来时如实 pytest.skip，不假绿。

"人已登录"的模拟方式（T10 常驻模型）：先起常驻无头守护，直接向守护
上下文种入 sessionid cookie（共享浏览器内存，后续命令附着同一守护——
T10 会话共享核心语义），再手写 meta.json（设计 §3 schema），之后完全
走 CLI 入口。结束由 _daemon_cleanup 兜底关守护、收驱动。
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")

from pageplay.cli import main  # noqa: E402
from pageplay.cookies import save_snapshot  # noqa: E402
from pageplay.recipes import save_recipe  # noqa: E402


@pytest.fixture
def seeded_site(fake_site, tmp_path, monkeypatch, _daemon_cleanup):
    """种好登录态的站点，返回 (site_name, home_root)。

    PAGEPLAY_HOME 指向临时目录；登录态种进常驻守护（内存共享），
    meta.json 手写——等价于 login 成功后的现场。
    """
    home = tmp_path / "pageplay-home"
    monkeypatch.setenv("PAGEPLAY_HOME", str(home))

    from pageplay import session as session_mod

    try:
        browser = session_mod.ensure_browser(headless=True)  # 无头守护不弹窗
        # expires 必须给：不带 expires 是会话级 cookie，守护重启会丢
        browser.contexts[0].add_cookies([
            {"name": "sessionid", "value": "test123", "url": fake_site,
             "expires": time.time() + 30 * 86400},
        ])
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过集成链：{exc}")

    site_dir = home / "sites" / "faketest"
    site_dir.mkdir(parents=True)

    hostname = urlsplit(fake_site).hostname or ""
    domain = ".".join(hostname.split(".")[-2:])  # 与 resolve_site 的注册域近似一致
    assert domain  # 种档用的注册域推导必须非空（meta 域名一致性）
    (site_dir / "meta.json").write_text(json.dumps({
        "site": "faketest",
        "login_url": fake_site + "/login",
        "saved_at": "2026-09-23T12:00:00",
        "check_cookies": ["sessionid"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "faketest", home


def test_login_doctor_export_chain(seeded_site, capsys):
    """全链：doctor 验活 0 且刷新快照 → export 输出含种下的 cookie。"""
    name, home = seeded_site
    site_dir = home / "sites" / name

    assert main(["doctor", name]) == 0
    out = capsys.readouterr().out
    assert "登录态有效" in out
    assert (site_dir / "state.json").is_file()  # doctor 成功后快照已刷新（设计 §3）

    assert main(["export", name]) == 0
    exported = capsys.readouterr().out
    assert "sessionid" in exported
    assert "test123" in exported


def test_export_to_out_file_content_and_permissions(home_dir, tmp_path, capsys):
    """export --out：文件内容为精简 JSON，权限 0600（不起浏览器，快照直供）。"""
    site_dir = home_dir / "sites" / "taobao"
    save_snapshot(site_dir, [{
        "name": "sessionid", "value": "test123", "domain": "taobao.com",
        "path": "/", "expires": -1, "httpOnly": False, "secure": False,
    }])
    out_file = tmp_path / "exported.json"

    assert main(["export", "taobao", "--out", str(out_file)]) == 0
    assert "已导出" in capsys.readouterr().out

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data == [{"name": "sessionid", "value": "test123",
                     "domain": "taobao.com", "expires": -1}]
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600


def test_run_recipe_end_to_end(seeded_site, table_site, tmp_path, capsys):
    """端到端：种登录态（守护内存共享）+ 手写 recipe → cli.main run → 真抓 5 行落盘。

    run --headless：附着已活的同款无头守护（doctor 种的登录态 run 直接
    复用——T10 会话共享），goto 表格假站（带种下的 cookie），等 #data
    出现后在页面上下文抽表、CSV+JSON 双份落 --out；产物内容逐行断言。
    测试走 --headless 不弹窗（有头链路归 test_daemon 的协作 e2e）。
    """
    name, home = seeded_site
    site_dir = home / "sites" / name
    save_recipe(site_dir, {
        "version": 1, "name": "faketest-1", "site": name,
        "url": table_site + "/table", "action": "table",
        "selector": "#data", "columns": None,
    })
    out_dir = tmp_path / "e2e-out"

    assert main(["run", "faketest-1", "--headless", "--out", str(out_dir)]) == 0

    json_files = list(out_dir.glob("faketest-1-*.json"))
    csv_files = list(out_dir.glob("faketest-1-*.csv"))
    assert len(json_files) == len(csv_files) == 1
    rows = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert len(rows) == 5
    assert rows[0]["名称"] == "商品1" and rows[4]["价格"] == "50"
    assert "已生成" in capsys.readouterr().out
