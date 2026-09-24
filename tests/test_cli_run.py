"""CLI 单测（run）：确定性重放、风控协作接线与账本落账，不起真浏览器。

T10：run 默认附着有头活窗（install_browser 记录 headless 实参），被弹回
登录页不直接停——协作函数 ensure_logged_in 在 cli_pick 命名空间打桩，
替身页面用 BounceRunPage 模拟"弹回→恢复"。真浏览器协作 e2e 归
test_daemon，真流程归 test_integration。
"""

from __future__ import annotations

import json

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pageplay.cli import main
from pageplay.cli_pick import _runs_path
from pageplay.runs import load_runs

from fakes_browser import (
    BounceRunPage,
    DownloadPage,
    RunPage,
    _TABLE,
    fake_downloads_home,
    install_browser,
    seed_recipe,
)


# ----------------------------------------------------------------------
# run：重放（✓/✗ 人话输出 + 执行账本落账）
# ----------------------------------------------------------------------

def test_run_table_recipe_saves_csv_and_json(home_dir, table_site, tmp_path,
                                             monkeypatch, capsys):
    """run 抓表：默认附着有头活窗（非 headless），CSV+JSON 各 5 行、列过滤生效，落账 ok。"""
    seed_recipe(home_dir, "faketest-table", table_site + "/table",
                "table", "#data", columns=["名称", "价格"])
    table = {"headers": ["名称", "价格", "库存", "链接"],
             "data": [[f"商品{i}", str(i * 10), "1", "x"] for i in range(1, 6)]}
    page = RunPage(table=table, landed_url=table_site + "/table")
    headless_calls = install_browser(monkeypatch, page)
    out_dir = tmp_path / "产物"

    assert main(["run", "faketest-table", "--out", str(out_dir)]) == 0

    assert headless_calls == [False]                       # 默认有头活窗
    assert page.goto_urls == [table_site + "/table"]
    assert page.closed is True                             # 完事关页
    csv_files = list(out_dir.glob("faketest-table-*.csv"))
    json_files = list(out_dir.glob("faketest-table-*.json"))
    assert len(csv_files) == len(json_files) == 1          # 文件名带时间戳
    rows = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert len(rows) == 5
    assert rows[0] == {"名称": "商品1", "价格": "10"}      # 只留勾选列
    out = capsys.readouterr().out
    assert str(csv_files[0].resolve()) in out
    assert "✓" in out and "（" in out and "B）" in out     # 路径带文件大小
    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "faketest-table" and entry["status"] == "ok"
    assert len(entry["outputs"]) == 2


def test_run_headless_flag_switches_daemon_mode(home_dir, table_site, tmp_path,
                                                monkeypatch, capsys):
    """--headless：ensure_browser 收到 headless=True（定时任务场景）。"""
    seed_recipe(home_dir, "faketest-table", table_site + "/table",
                "table", "#data")
    page = RunPage(table=_TABLE, landed_url=table_site + "/table")
    headless_calls = install_browser(monkeypatch, page)

    assert main(["run", "faketest-table", "--headless",
                 "--out", str(tmp_path)]) == 0
    assert headless_calls == [True]


# ----------------------------------------------------------------------
# run：登录反弹协作（重试上限 2 次，人话报错）
# ----------------------------------------------------------------------

_TARGET = "https://sycm.taobao.com/mq/order/table"
_LOGIN = "https://login.taobao.com/member/login.jhtml"


def test_run_login_bounce_collaborates_then_succeeds(home_dir, tmp_path,
                                                     monkeypatch, capsys):
    """被弹回登录页：协作（人过验证的替身）→ 自动续跑抓表成功，不直接停。"""
    seed_recipe(home_dir, "faketest-bounce", _TARGET, "table", "#data")
    page = BounceRunPage(_TARGET, _LOGIN)
    install_browser(monkeypatch, page)
    collab_calls: list[dict] = []

    def fake_collab(browser, site, max_wait=120):
        collab_calls.append({"site": site.name, "max_wait": max_wait})
        page.recovered = True  # = 人在窗口里完成了登录
        return True

    monkeypatch.setattr("pageplay.cli_pick.ensure_logged_in", fake_collab)

    assert main(["run", "faketest-bounce", "--out", str(tmp_path)]) == 0

    assert page.goto_urls == [_TARGET, _TARGET]  # 首次被弹回 + 通过后重试
    assert len(collab_calls) == 1 and collab_calls[0]["site"] == "faketest"
    out = capsys.readouterr().out
    assert "✓" in out and "5 行" in out
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "ok"


def test_run_login_bounce_exceeds_retry_limit_fails_with_human_words(
        home_dir, tmp_path, monkeypatch, capsys):
    """一直被弹回：协作 2 次后放弃，✗ 人话 + 一条 fail 账，退出码 1。"""
    seed_recipe(home_dir, "faketest-stuck", _TARGET, "table", "#data")
    page = BounceRunPage(_TARGET, _LOGIN)
    install_browser(monkeypatch, page)
    collab_calls: list[int] = []

    def fake_collab(browser, site, max_wait=120):
        collab_calls.append(1)
        return True  # 每次都"通过"，但页面仍弹回（验证没真正生效）

    monkeypatch.setattr("pageplay.cli_pick.ensure_logged_in", fake_collab)

    assert main(["run", "faketest-stuck", "--out", str(tmp_path)]) == 1

    assert len(collab_calls) == 2              # 重试上限 2 次
    assert page.goto_urls == [_TARGET] * 3     # 首次 + 2 次重试
    err = capsys.readouterr().err
    assert "登录态失效/被弹回登录页" in err and "已重试 2 次" in err
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail" and "被弹回登录页" in entry["detail"]


def test_run_login_bounce_collab_timeout_fails(home_dir, tmp_path, monkeypatch,
                                               capsys):
    """协作等人超时（或无头守护）：✗ 人话 + 一条 fail 账，退出码 1。"""
    seed_recipe(home_dir, "faketest-wait", _TARGET, "table", "#data")
    page = BounceRunPage(_TARGET, _LOGIN)
    install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.cli_pick.ensure_logged_in",
                        lambda browser, site, max_wait=120: False)

    assert main(["run", "faketest-wait", "--out", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "登录态失效/被弹回登录页" in err and "请重跑本命令" in err
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail"


def test_run_guard_hit_on_non_login_page_still_exit_two(home_dir, tmp_path,
                                                        monkeypatch, capsys):
    """真风控（非登录页命中关键词）：照旧停机，退出码 2。"""
    seed_recipe(home_dir, "faketest-risk", "https://demo.example.com/table",
                "table", "#data")
    page = RunPage(table=_TABLE,
                   landed_url="https://demo.example.com/table",
                   content="<html><body>异常请求，请输入验证码</body></html>")
    install_browser(monkeypatch, page)

    assert main(["run", "faketest-risk", "--out", str(tmp_path)]) == 2
    assert "风控" in capsys.readouterr().err
    assert not (home_dir / "runs.jsonl").exists()  # 与旧行为一致：不落账


# ----------------------------------------------------------------------
# run：download 档与失败路径
# ----------------------------------------------------------------------

def test_run_download_recipe_saves_file(home_dir, table_site, tmp_path,
                                        monkeypatch, capsys):
    """run 下载档：点击触发下载，文件按建议文件名落盘且内容一致，落账 ok。"""
    seed_recipe(home_dir, "faketest-dl", table_site + "/download",
                "download", "#dl")
    page = DownloadPage()
    install_browser(monkeypatch, page)
    out_dir = tmp_path / "dl-out"

    assert main(["run", "faketest-dl", "--out", str(out_dir)]) == 0

    assert page.clicked == ["#dl"]
    saved = out_dir / "report.xlsx"
    assert saved.read_bytes() == b"fake-xlsx-content"
    out = capsys.readouterr().out
    assert "✓" in out and str(saved.resolve()) in out
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "ok" and entry["outputs"] == [str(saved.resolve())]


def test_run_download_without_download_records_fail(home_dir, table_site,
                                                    tmp_path, monkeypatch,
                                                    capsys):
    """run 下载无下载事件：✗ 人话 + 一条 fail 账，退出码 1。"""
    seed_recipe(home_dir, "faketest-nodl", table_site + "/download",
                "download", "#dead")
    page = DownloadPage(
        download_error=PlaywrightTimeoutError("Timeout 30000ms exceeded"))
    install_browser(monkeypatch, page)

    assert main(["run", "faketest-nodl", "--out", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "✗" in err and "没有触发文件下载" in err
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail" and entry["outputs"] == []
    assert "没有触发文件下载" in entry["detail"]


def test_run_selector_timeout_prompts_repick(home_dir, table_site, monkeypatch,
                                             capsys):
    """选择器 15s 等不到：提示"重新 pick"退出码 1，并落一条 fail 账。"""
    seed_recipe(home_dir, "faketest-stale", table_site + "/table",
                "table", "#gone")
    page = RunPage(wait_error=PlaywrightTimeoutError("Timeout 15000ms exceeded"),
                   landed_url=table_site + "/table")
    install_browser(monkeypatch, page)

    assert main(["run", "faketest-stale"]) == 1

    assert "重新 pick" in capsys.readouterr().err
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail" and "重新 pick" in entry["detail"]


def test_run_unknown_recipe_lists_existing(home_dir, capsys):
    """名字双查都落空：人话列出现有流程与 recipe；没执行不落账。"""
    seed_recipe(home_dir, "faketest-table", "https://x.example.com/table",
                "table", "#data")

    assert main(["run", "no-such"]) == 1

    err = capsys.readouterr().err
    assert "既不是已保存的流程" in err and "faketest-table" in err
    assert "现有流程" in err and "现有 recipe" in err
    assert not (home_dir / "runs.jsonl").exists()  # 没执行就没有执行记录
