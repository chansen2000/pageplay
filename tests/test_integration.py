"""集成测试：真浏览器（Playwright chromium）跑通 种登录态 → doctor → export 全链。

前置：.venv/bin/python -m playwright install chromium
内核/驱动起不来时如实 pytest.skip，不假绿。

"人已登录"的模拟方式：起临时 persistent context → add_cookies 种入
sessionid=test123 → close 落盘 → 把该档案目录移入 PAGEPLAY_HOME 站点目录
命名 browser-profile，再手写 meta.json（设计 §3 schema）——等价于 login
成功后的现场，之后完全走 CLI 入口。
"""

from __future__ import annotations

import json
import shutil
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright", reason="未安装 playwright 包")
from playwright.sync_api import sync_playwright  # noqa: E402

from pageplay.cli import main  # noqa: E402
from pageplay.cookies import save_snapshot  # noqa: E402


@pytest.fixture
def seeded_site(fake_site, tmp_path, monkeypatch):
    """种好登录态的站点，返回 (site_name, home_root)。

    PAGEPLAY_HOME 指向 tmp；浏览器档案从独立 seed 目录移入
    <home>/sites/faketest/browser-profile，meta.json 手写。
    """
    home = tmp_path / "pageplay-home"
    monkeypatch.setenv("PAGEPLAY_HOME", str(home))
    seed_profile = tmp_path / "seed-profile"

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(seed_profile), headless=True)
            try:
                # expires 必须给：不带 expires 是会话级 cookie，Chromium
                # 关闭进程时不落盘，搬档案后登录态就丢了
                context.add_cookies([
                    {"name": "sessionid", "value": "test123", "url": fake_site,
                     "expires": time.time() + 30 * 86400},
                ])
            finally:
                context.close()
    except Exception as exc:  # 内核缺失/驱动起不来：如实跳过，不假绿
        pytest.skip(f"playwright chromium 不可用，跳过集成链：{exc}")

    site_dir = home / "sites" / "faketest"
    site_dir.mkdir(parents=True)
    shutil.move(str(seed_profile), str(site_dir / "browser-profile"))

    hostname = urlsplit(fake_site).hostname or ""
    domain = ".".join(hostname.split(".")[-2:])  # 与 resolve_site 的注册域近似一致
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
