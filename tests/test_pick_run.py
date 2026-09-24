"""CLI 单测（pick/results/recipes）：确认即执行、会话摘要与账本回看，不起真浏览器。

T10 后 pick/run 直接经 cli_pick 的浏览器附着函数干活，替身浏览器/页面与
附着打桩统一在 fakes_browser（install_browser）；run 侧测试在 test_cli_run。
确认即执行：pick 替身确认会真调 on_confirm（与真 picker 同形状），确认钩子
当场执行；产物目录靠 monkeypatch HOME 指到临时盘。真流程归
test_daemon / test_integration。
"""

from __future__ import annotations

import json

from pageplay import picker
from pageplay.cli import main
from pageplay.cli_pick import _runs_path
from pageplay.recipes import save_recipe
from pageplay.runs import load_runs, record_run

from fakes_browser import PickPage, fake_downloads_home, install_browser
from test_cli import _write_saved_site


# ----------------------------------------------------------------------
# pick：确认即执行（存 recipe + 当场执行 + ✓/✗ 显示 + 落账 + 封面）
# ----------------------------------------------------------------------

def test_pick_confirm_table_executes_immediately(home_dir, table_site, tmp_path,
                                                 monkeypatch, capsys):
    """确认即执行：替身确认一张表 → 产物当场落地 + runs.jsonl 一条 ok + ✓ 与路径。"""
    _write_saved_site(home_dir, name="faketest")
    fake_home = fake_downloads_home(monkeypatch, tmp_path)
    page = PickPage(table_site + "/table", table={
        "headers": ["名称", "价格", "库存", "链接"],
        "data": [[f"商品{i}", str(i * 10), "1", "x"] for i in range(1, 6)]})
    install_browser(monkeypatch, page)

    def fake_run_pick(page, on_confirm, repeat=False):
        payload = {"selector": "#data", "action": "table",
                   "columns": ["名称", "价格"], "rect": {"x": 1}}
        on_confirm(payload)  # 与真 picker 同形状：确认即回调
        return [payload]

    monkeypatch.setattr("pageplay.picker.run_pick", fake_run_pick)
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))

    assert main(["pick", "faketest", "--name", "my-recipe"]) == 0

    # 产物当场在默认目录 ~/Downloads/pageplay/<名>/ 落地
    products = fake_home / "Downloads" / "pageplay" / "my-recipe"
    csv_files = sorted(products.glob("my-recipe-*.csv"))
    json_files = sorted(products.glob("my-recipe-*.json"))
    assert len(csv_files) == len(json_files) == 1
    assert "商品1" in csv_files[0].read_text(encoding="utf-8-sig")
    # 执行账本：一条 ok，产物为绝对路径
    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "my-recipe" and entry["action"] == "table"
    assert entry["status"] == "ok" and "行" in entry["detail"]
    assert len(entry["outputs"]) == 2
    assert all(str(products) in o for o in entry["outputs"])
    # 显示：✓ + 行数 + 绝对路径；recipe 照存；会话摘要给出名字与产物目录
    out = capsys.readouterr().out
    assert "已锁定 table：#data" in out
    assert "✓" in out and "5 行" in out
    assert str(csv_files[0].resolve()) in out
    assert "本次框选会话结束：共收 1 条" in out
    assert "my-recipe → 以后一条命令重放：pageplay run my-recipe" in out
    assert "产物目录" in out
    assert page.closed is True  # 完事关页（浏览器不动）
    recipe = json.loads((home_dir / "sites" / "faketest" / "recipes"
                         / "my-recipe.json").read_text(encoding="utf-8"))
    assert recipe["action"] == "table" and recipe["columns"] == ["名称", "价格"]


def test_pick_repeat_two_confirms_saves_two_recipes_and_summary(
        home_dir, table_site, tmp_path, monkeypatch, capsys):
    """repeat 会话：替身确认两条 → 两条 recipe 各自落盘落账，摘要列两条名字。"""
    _write_saved_site(home_dir, name="faketest")
    fake_downloads_home(monkeypatch, tmp_path)
    page = PickPage(table_site + "/table", table={
        "headers": ["名称", "价格", "库存", "链接"],
        "data": [[f"商品{i}", str(i * 10), "1", "x"] for i in range(1, 6)]})
    install_browser(monkeypatch, page)

    def fake_run_pick(page, on_confirm, repeat=False):
        assert repeat is True  # T9c：pick 以会话常驻跑
        first = {"selector": "#data", "action": "table",
                 "columns": ["名称"], "rect": {"x": 1}}
        second = {"selector": "html > body > a", "action": "download",
                  "columns": None, "rect": {"x": 2}}
        on_confirm(first)
        on_confirm(second)
        return [first, second]

    monkeypatch.setattr("pageplay.picker.run_pick", fake_run_pick)
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))

    assert main(["pick", "faketest", "--name", "daily"]) == 0

    recipes_dir = home_dir / "sites" / "faketest" / "recipes"
    first = json.loads((recipes_dir / "daily.json").read_text(encoding="utf-8"))
    second = json.loads(
        (recipes_dir / "daily-2.json").read_text(encoding="utf-8"))
    assert first["action"] == "table" and second["action"] == "download"
    entries = load_runs(_runs_path())
    assert [e["recipe"] for e in entries] == ["daily-2", "daily"]  # 新在前
    by_name = {e["recipe"]: e["status"] for e in entries}
    assert by_name == {"daily": "ok", "daily-2": "fail"}  # download 替身页无下载 → ✗
    out = capsys.readouterr().out
    assert "本次框选会话结束：共收 2 条" in out
    assert "daily →" in out and "daily-2 →" in out
    assert "pageplay run daily" in out and "pageplay run daily-2" in out


def test_pick_confirm_download_without_download_fails_and_continues(
        home_dir, table_site, tmp_path, monkeypatch, capsys):
    """download 无下载触发：✗ 人话 + runs.jsonl 一条 fail + 会话不崩、recipe 照存。"""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from fakes_browser import DownloadPage

    _write_saved_site(home_dir, name="faketest")
    fake_downloads_home(monkeypatch, tmp_path)
    page = DownloadPage(
        download_error=PlaywrightTimeoutError(
            "Timeout 30000ms exceeded while waiting for event 'download'"))
    install_browser(monkeypatch, page)

    def fake_run_pick(page, on_confirm, repeat=False):
        payload = {"selector": "#dl", "action": "download", "columns": None}
        on_confirm(payload)
        return [payload]

    monkeypatch.setattr("pageplay.picker.run_pick", fake_run_pick)
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))

    assert main(["pick", "faketest", "--name", "dl-recipe"]) == 0  # 会话不崩

    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail" and entry["recipe"] == "dl-recipe"
    assert "没有触发文件下载" in entry["detail"] and entry["outputs"] == []
    captured = capsys.readouterr()  # readouterr 会排空缓冲，只读一次
    assert "✗" in captured.err and "没有触发文件下载" in captured.err
    assert "共收 1 条" in captured.out  # 失败后照走完会话摘要


def test_pick_full_flow_saves_recipe_and_cover(home_dir, tmp_path, monkeypatch,
                                               capsys):
    """pick 全流程：--name 生效，recipe json + 封面 png 落盘，摘要给 run 指引。"""
    _write_saved_site(home_dir, name="faketest")
    fake_downloads_home(monkeypatch, tmp_path)
    page = PickPage("https://demo.example.com/report", table={
        "headers": ["名称"], "data": [["商品1"], ["商品2"]]})  # 有表数据：执行成功才截封面
    install_browser(monkeypatch, page)

    def fake_run_pick(page, on_confirm, repeat=False):
        payload = {"selector": "#data", "action": "table",
                   "columns": ["名称"], "rect": {"x": 1}}
        on_confirm(payload)
        return [payload]

    monkeypatch.setattr("pageplay.picker.run_pick", fake_run_pick)
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))

    assert main(["pick", "faketest", "--name", "my-recipe"]) == 0

    recipe = json.loads((home_dir / "sites" / "faketest" / "recipes"
                         / "my-recipe.json").read_text(encoding="utf-8"))
    assert recipe["version"] == 1
    assert recipe["name"] == "my-recipe" and recipe["site"] == "faketest"
    assert recipe["url"] == "https://demo.example.com/report"  # 实际落点
    assert recipe["action"] == "table" and recipe["selector"] == "#data"
    assert recipe["columns"] == ["名称"]
    cover = home_dir / "sites" / "faketest" / "recipes" / "my-recipe.png"
    assert cover.read_bytes() == b"png"
    assert page.goto_urls == ["https://demo.example.com/login"]  # 开 home_url
    out = capsys.readouterr().out
    assert "已锁定 table：#data" in out      # on_confirm 钩子回显
    assert "pageplay run my-recipe" in out   # 摘要里的 run 指引


def test_pick_default_name_counts_existing_recipes(home_dir, tmp_path,
                                                   monkeypatch, capsys):
    """无 --name：默认名 = <站点>-<已有 recipe 数 + 1>；download 列为 None。"""
    _write_saved_site(home_dir, name="faketest")
    fake_downloads_home(monkeypatch, tmp_path)
    install_browser(monkeypatch, PickPage("https://demo.example.com/x"))
    monkeypatch.setattr("pageplay.picker.cover_screenshot",
                        lambda page, selector, out: out.write_bytes(b"png"))

    def fake_run_pick(page, on_confirm, repeat=False):
        payload = {"selector": "#t", "action": "download", "columns": None}
        on_confirm(payload)
        return [payload]

    monkeypatch.setattr("pageplay.picker.run_pick", fake_run_pick)
    save_recipe(home_dir / "sites" / "faketest", {
        "version": 1, "name": "faketest-1", "site": "faketest",
        "url": "https://demo.example.com/x", "action": "table",
        "selector": "#t", "columns": None})

    assert main(["pick", "faketest"]) == 0

    recipe = json.loads((home_dir / "sites" / "faketest" / "recipes"
                         / "faketest-2.json").read_text(encoding="utf-8"))
    assert recipe["name"] == "faketest-2"    # 已有 1 个 → 序号 2
    assert recipe["action"] == "download" and recipe["columns"] is None


def test_pick_cancelled_returns_one(home_dir, monkeypatch, capsys):
    """一条没收（Esc/关窗）：打印"已取消"退出码 1。"""
    _write_saved_site(home_dir, name="faketest")
    install_browser(monkeypatch, PickPage("https://x.example.com/1"))

    def cancelled(page, on_confirm, repeat=False):
        raise picker.PickCancelled("人按 Esc 取消了框选")

    monkeypatch.setattr("pageplay.picker.run_pick", cancelled)

    assert main(["pick", "faketest"]) == 1
    assert "已取消" in capsys.readouterr().out


# ----------------------------------------------------------------------
# results：执行账本回看
# ----------------------------------------------------------------------

def _seed_runs(*details: str) -> None:
    """按顺序落几条账（detail 作身份标记，created_at 定死保证顺序确定）。"""
    for i, detail in enumerate(details, start=1):
        record_run(_runs_path(), {
            "recipe": f"r{i % 2 + 1}", "action": "table",
            "status": "ok" if i % 2 else "fail",
            "detail": detail,
            "outputs": [f"/tmp/p{i}.csv"] if i % 2 else [],
            "created_at": f"2026-09-24T00:00:0{i}"})


def test_results_lists_recent_runs_newest_first(home_dir, capsys):
    """results：新在前列出 时间/recipe/动作/✓✗；成功行给产物路径，失败行给人话原因。"""
    _seed_runs("第一条", "第二条", "第三条")

    assert main(["results"]) == 0

    out = capsys.readouterr().out
    # 新在前：最新时间戳出现在最前（ok 行展示产物路径，不展示 detail）
    assert out.index("2026-09-24T00:00:03") < out.index("2026-09-24T00:00:01")
    assert "✓" in out and "✗" in out
    assert "/tmp/p3.csv" in out                         # 成功行显示产物路径
    assert "第二条" in out                              # 失败行显示失败原因


def test_results_filter_recipe_and_limit(home_dir, capsys):
    """results r1 --limit 1：只看该 recipe、只取最近一条。"""
    _seed_runs("第一条", "第二条", "第三条")  # r2, r1, r2

    assert main(["results", "r1", "--limit", "1"]) == 0

    out = capsys.readouterr().out
    assert "第二条" in out and "第一条" not in out and "第三条" not in out


def test_results_empty_home_prints_hint(home_dir, capsys):
    """空态：没有任何执行记录时给指引，退出码 0。"""
    home_dir.mkdir(parents=True)

    assert main(["results"]) == 0
    assert "暂无执行记录" in capsys.readouterr().out


# ----------------------------------------------------------------------
# recipes：清单（不变行为）
# ----------------------------------------------------------------------

def test_recipes_lists_name_action_url_created(home_dir, capsys):
    """recipes：列名字/动作/URL/创建时间；带站点名只列该站。"""
    save_recipe(home_dir / "sites" / "faketest", {
        "version": 1, "name": "faketable", "site": "faketest",
        "url": "https://demo.example.com/table", "action": "table",
        "selector": "#data", "columns": ["名称"]})

    assert main(["recipes"]) == 0
    out = capsys.readouterr().out
    assert "faketable" in out and "table" in out
    assert "https://demo.example.com/table" in out
    assert "创建于" in out

    assert main(["recipes", "taobao"]) == 0   # taobao 无 recipe → 只见空态提示
    out2 = capsys.readouterr().out
    assert "faketable" not in out2 and "暂无 recipe" in out2


def test_recipes_empty_home_prints_hint(home_dir, capsys):
    """空态：一个 recipe 都没有时给 pick 指引，退出码 0。"""
    home_dir.mkdir(parents=True)

    assert main(["recipes"]) == 0
    assert "暂无 recipe" in capsys.readouterr().out
