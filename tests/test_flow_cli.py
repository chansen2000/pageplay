"""CLI 单测（T11d 流程接线）：record 录制落盘、run 双查整链重放、flows 清单。

不起真浏览器：浏览器附着用 install_browser 替身，录制会话 record_session
与整链 run_flow 按各自 import 位置打桩（record 收获步的"当场执行"走真
actions 路径，只替身页面回应）。recipe 单条路径的零回归归 test_cli_run。
"""

from __future__ import annotations

import json

import pytest

from pageplay.cli import main
from pageplay.cli_pick import _runs_path
from pageplay.flows import load_flow, save_flow
from pageplay.guard import RiskTriggered
from pageplay.picker import PickCancelled
from pageplay.recipes import save_recipe
from pageplay.runs import load_runs, record_run

from fakes_browser import (
    PickPage,
    RunPage,
    fake_downloads_home,
    install_browser,
)
from test_cli import _write_saved_site

_START = "https://demo.example.com/home"

_TABLE = {"headers": ["名称", "价格", "库存", "链接"],
          "data": [[f"商品{i}", str(i * 10), "1", "x"] for i in range(1, 6)]}

_CLICK_STEP = {"no": 1, "kind": "click", "url": _START,
               "selector": "#to-list", "note": ""}
_HARVEST_STEP = {"no": 2, "kind": "table", "url": _START,
                 "selector": "#t1", "columns": ["名称"], "note": ""}


class RecordPage(PickPage):
    """record 替身页面：补 content()（goto 后过 Guard 用），其余同 pick。"""

    def content(self) -> str:
        return "<html><body>正常页面内容</body></html>"


def _seed_flow(home_dir, name: str, site: str = "faketest",
               url: str = _START, steps: list | None = None) -> None:
    _write_saved_site(home_dir, name=site)  # flows <站> 需要站点可解析
    save_flow(home_dir / "sites" / site, {
        "version": 1, "name": name, "site": site, "url": url,
        "steps": steps or [{"no": 1, "kind": "goto", "url": url},
                           {"no": 2, "kind": "click", "url": url,
                            "selector": "#to-list", "note": ""}],
    })


# ----------------------------------------------------------------------
# record：录制 → 收获当场执行 → 起名落盘
# ----------------------------------------------------------------------

def test_record_saves_flow_and_executes_harvest(home_dir, tmp_path,
                                                monkeypatch, capsys):
    """录制两步（click + table 收获）：收获当场执行落产物，流程落盘可重放。"""
    _write_saved_site(home_dir)
    fake_home = fake_downloads_home(monkeypatch, tmp_path)
    page = RecordPage(_START, table=_TABLE)
    install_browser(monkeypatch, page)
    draft_dir = fake_home / "Downloads" / "pageplay" / "faketest-flow" / "record"

    def fake_record_session(p, on_step, on_harvest):
        assert p is page
        on_step(_CLICK_STEP)
        on_harvest(_HARVEST_STEP)  # 框选确认 → 当场执行
        return [dict(_CLICK_STEP), dict(_HARVEST_STEP)]

    monkeypatch.setattr("pageplay.recorder.record_session",
                        fake_record_session)
    monkeypatch.setattr("builtins.input", lambda *a: "my-flow")

    assert main(["record", "faketest"]) == 0

    flow = load_flow(home_dir / "sites" / "faketest", "my-flow")
    assert flow["url"] == _START                       # 流程实际起始 URL
    assert [s["kind"] for s in flow["steps"]] == ["click", "table"]
    products = sorted(draft_dir.glob("faketest-flow-*.json"))
    assert len(products) == 1                          # 收获步当场落了产物
    out = capsys.readouterr().out
    assert "已记步骤 1：click #to-list" in out
    assert "✓" in out and "抓表 5 行" in out           # 收获执行回显
    assert "流程已保存：my-flow" in out and "2 步" in out
    assert "pageplay run my-flow" in out
    (entry,) = load_runs(_runs_path())                 # 收获落账（占位名）
    assert entry["recipe"] == "faketest-flow" and entry["status"] == "ok"


def test_record_enter_uses_next_flow_name_and_name_flag_skips_prompt(
        home_dir, tmp_path, monkeypatch, capsys):
    """回车 = <站>-flow-N 默认名；--name 直用不再问输入。"""
    _write_saved_site(home_dir)
    fake_downloads_home(monkeypatch, tmp_path)
    page = RecordPage(_START, table=_TABLE)
    install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.recorder.record_session",
                        lambda p, s, h: [dict(_CLICK_STEP)])

    def no_input(*a):  # --name 给定时不应再问
        raise AssertionError("不该问流程名")

    monkeypatch.setattr("builtins.input", no_input)
    assert main(["record", "faketest", "--name", "given"]) == 0
    assert load_flow(home_dir / "sites" / "faketest", "given")["name"] == "given"

    monkeypatch.setattr("builtins.input", lambda *a: "")   # 回车用默认名
    assert main(["record", "faketest"]) == 0
    assert load_flow(home_dir / "sites" / "faketest",
                     "faketest-flow-1")["name"] == "faketest-flow-1"


def test_record_no_steps_and_cancelled_exit_one(home_dir, tmp_path,
                                                monkeypatch, capsys):
    """一条没收（空返回 / PickCancelled）：人话 + 退出码 1。"""
    _write_saved_site(home_dir)
    fake_downloads_home(monkeypatch, tmp_path)
    page = RecordPage(_START, table=_TABLE)
    install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.recorder.record_session", lambda p, s, h: [])
    assert main(["record", "faketest"]) == 1
    assert "未记录任何操作" in capsys.readouterr().out

    def cancelled(p, s, h):
        raise PickCancelled("人 Ctrl-C 结束了录制")

    monkeypatch.setattr("pageplay.recorder.record_session", cancelled)
    assert main(["record", "faketest"]) == 1
    assert "已取消" in capsys.readouterr().out


def test_record_goto_bounce_reports_and_closes_page(home_dir, tmp_path,
                                                    monkeypatch, capsys):
    """起始页被弹回登录页且协作未通过：✗ 人话、退出码 1、页已收。"""
    _write_saved_site(home_dir)
    fake_downloads_home(monkeypatch, tmp_path)
    page = RecordPage(_START, table=_TABLE)
    install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.cli_pick._goto_with_login_recovery",
                        lambda *a, **k: "登录态失效/被弹回登录页，请重跑本命令")
    monkeypatch.setattr("pageplay.recorder.record_session",
                        lambda p, s, h: [_CLICK_STEP])

    assert main(["record", "faketest"]) == 1

    assert "登录态失效" in capsys.readouterr().err
    assert page.closed is True                          # finally 收页


@pytest.mark.parametrize("prompt_error", [
    EOFError("无终端可输入"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
])
def test_record_prompt_error_autosaves_with_default_name(
        home_dir, tmp_path, monkeypatch, capsys, prompt_error):
    """T13.1：起名处 EOF/解码失败（GUI/管道场景）→ 自动命名照常落盘。

    以前这里是丢弃录制退出 1；现在改为用 next_flow_name 默认名保存并
    明示，退出码 0——录制成果一分钟都不能丢。
    """
    _write_saved_site(home_dir)
    fake_downloads_home(monkeypatch, tmp_path)
    page = RecordPage(_START, table=_TABLE)
    install_browser(monkeypatch, page)

    def fake_record_session(p, on_step, on_harvest):
        on_step(_CLICK_STEP)
        return [dict(_CLICK_STEP), dict(_HARVEST_STEP)]
    monkeypatch.setattr("pageplay.recorder.record_session",
                        fake_record_session)

    def no_input(*a):
        raise prompt_error

    monkeypatch.setattr("builtins.input", no_input)
    assert main(["record", "faketest"]) == 0
    flow = load_flow(home_dir / "sites" / "faketest", "faketest-flow-1")
    assert [s["kind"] for s in flow["steps"]] == ["click", "table"]
    out = capsys.readouterr().out
    assert "自动命名" in out and "faketest-flow-1" in out
    assert "流程已保存：faketest-flow-1" in out


# ----------------------------------------------------------------------
# run 双查：流程命中走整链（runner 打桩），未命中回落 recipe
# ----------------------------------------------------------------------

def test_run_flow_by_name_replays_and_records(home_dir, tmp_path,
                                              monkeypatch, capsys):
    """名字命中流程：runner.run_flow 整链重放，一条 run 账带步摘要与产物。"""
    _seed_flow(home_dir, "myflow")
    out_dir = tmp_path / "产物"
    page = RecordPage(_START)
    headless_calls = install_browser(monkeypatch, page)

    def fake_run_flow(browser, flow, products_dir, *, stem=None):
        assert stem == "myflow" and flow["name"] == "myflow"
        assert str(products_dir) == str(out_dir)
        products_dir.mkdir(parents=True, exist_ok=True)   # 模拟产物落盘
        (products_dir / "myflow-2.csv").write_text("名称\n商品1", encoding="utf-8")
        (products_dir / "myflow-2.json").write_text("[{}]", encoding="utf-8")
        return {"ok": True, "failed_step": None, "results": [
            {"no": 1, "kind": "goto", "status": "ok", "detail": "已打开 x"},
            {"no": 2, "kind": "table", "status": "ok", "detail": "已生成表格"},
        ]}

    monkeypatch.setattr("pageplay.runner.run_flow", fake_run_flow)

    assert main(["run", "myflow", "--out", str(out_dir)]) == 0

    assert headless_calls == [False]                    # 默认附着有头活窗
    out = capsys.readouterr().out
    assert "✓ 流程 myflow：2 步全部完成" in out
    assert str((out_dir / "myflow-2.csv").resolve()) in out
    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "myflow" and entry["action"] == "flow"
    assert entry["status"] == "ok" and "第1步" in entry["detail"]
    assert len(entry["outputs"]) == 2                   # 快照对比点数产物


def test_run_flow_headless_passthrough(home_dir, tmp_path, monkeypatch):
    """--headless 透传给 ensure_browser（定时任务场景）。"""
    _seed_flow(home_dir, "myflow")
    page = RecordPage(_START)
    headless_calls = install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.runner.run_flow",
                        lambda *a, **k: {"ok": True, "failed_step": None,
                                         "results": []})
    assert main(["run", "myflow", "--headless", "--out", str(tmp_path)]) == 0
    assert headless_calls == [True]


def test_run_flow_failure_reports_step_and_records_fail(home_dir, tmp_path,
                                                        monkeypatch, capsys):
    """流程第 2 步失败：✗ 第 N 步 + 人话，一条 fail 账，退出码 1。"""
    _seed_flow(home_dir, "stuck")
    page = RecordPage(_START)
    install_browser(monkeypatch, page)

    def fake_run_flow(browser, flow, out_dir, *, stem=None):
        return {"ok": False, "failed_step": 2, "results": [
            {"no": 1, "kind": "goto", "status": "ok", "detail": "已打开 x"},
            {"no": 2, "kind": "click", "status": "fail",
             "detail": "第 2 步卡住：页面结构可能变了"},
        ]}

    monkeypatch.setattr("pageplay.runner.run_flow", fake_run_flow)

    assert main(["run", "stuck", "--out", str(tmp_path)]) == 1

    assert "第 2 步" in capsys.readouterr().err
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail" and "第2步" in entry["detail"]


def test_run_flow_risk_hit_propagates_exit_two(home_dir, tmp_path,
                                               monkeypatch):
    """流程中途真风控：RiskTriggered 向上穿透，main 映射退出码 2。"""
    _seed_flow(home_dir, "risky")
    install_browser(monkeypatch, RecordPage(_START))

    def boom(*a, **k):
        raise RiskTriggered("检测到滑块验证")

    monkeypatch.setattr("pageplay.runner.run_flow", boom)
    assert main(["run", "risky", "--out", str(tmp_path)]) == 2


def test_run_name_miss_lists_flows_and_recipes(home_dir, capsys):
    """名字双查都落空：人话同时列出可用流程与 recipe；不落账。"""
    _seed_flow(home_dir, "known-flow")
    (home_dir / "runs.jsonl").unlink(missing_ok=True)

    assert main(["run", "no-such"]) == 1

    err = capsys.readouterr().err
    assert "known-flow" in err and "现有流程" in err and "现有 recipe" in err
    assert not (home_dir / "runs.jsonl").exists()


# ----------------------------------------------------------------------
# flows：清单命令
# ----------------------------------------------------------------------

def test_flows_lists_all_sites_and_single_site(home_dir, capsys):
    """无参列全部站、带参只列该站：名字/步数/起始URL/创建时间。"""
    _seed_flow(home_dir, "a-flow")
    _seed_flow(home_dir, "b-flow", site="other")

    assert main(["flows"]) == 0
    out = capsys.readouterr().out
    assert "a-flow" in out and "b-flow" in out and "2 步" in out
    assert _START in out and "创建于" in out

    assert main(["flows", "faketest"]) == 0
    out = capsys.readouterr().out
    assert "a-flow" in out and "b-flow" not in out

    assert main(["flows", "other"]) == 0
    assert "b-flow" in capsys.readouterr().out


def test_flows_empty_gives_record_hint(home_dir, capsys):
    """空态：给 record 指引，退出码 0。"""
    assert main(["flows"]) == 0
    out = capsys.readouterr().out
    assert "暂无流程" in out and "pageplay record" in out


# ----------------------------------------------------------------------
# --json：机读输出（T12 GUI 下拉数据源）
# ----------------------------------------------------------------------

def test_json_outputs_parse_with_expected_fields(home_dir, capsys):
    """四个清单命令 --json：stdout 是 JSON 数组，字段与文本版对齐。"""
    _seed_flow(home_dir, "j-flow")
    save_recipe(home_dir / "sites" / "faketest", {
        "version": 1, "name": "j-recipe", "site": "faketest", "url": _START,
        "action": "table", "selector": "#t1"})
    record_run(_runs_path(), {"recipe": "j-recipe", "action": "table",
                              "status": "ok", "detail": "已生成", "outputs": []})

    assert main(["list", "--json"]) == 0
    sites = json.loads(capsys.readouterr().out)
    assert isinstance(sites, list) and sites
    assert {"name", "saved_at"} <= set(sites[0])
    assert any(s["name"] == "faketest" for s in sites)

    assert main(["flows", "--json"]) == 0
    (flow,) = json.loads(capsys.readouterr().out)
    assert flow["name"] == "j-flow" and flow["step_count"] == 2
    assert {"name", "url", "step_count", "created_at"} == set(flow)

    assert main(["recipes", "--json"]) == 0
    (recipe,) = json.loads(capsys.readouterr().out)
    assert recipe["name"] == "j-recipe" and recipe["action"] == "table"
    assert {"name", "action", "url", "created_at"} == set(recipe)

    assert main(["results", "--json"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)
    assert entry["recipe"] == "j-recipe" and entry["status"] == "ok"
    assert {"recipe", "action", "status", "detail", "outputs",
            "created_at"} == set(entry)


def test_json_empty_outputs_empty_array(home_dir, capsys):
    """空库 --json：flows/results 输出 []（GUI 空态数据源）。"""
    assert main(["flows", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["results", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_json_keeps_non_ascii_and_text_path_unchanged(home_dir, capsys):
    """--json 不转义中文（ensure_ascii=False）；不带 --json 文本输出照旧。"""
    save_flow(home_dir / "sites" / "faketest", {
        "version": 1, "name": "中文流程", "site": "faketest", "url": _START,
        "steps": [{"no": 1, "kind": "goto", "url": _START}]})

    assert main(["flows", "--json"]) == 0
    raw = capsys.readouterr().out
    assert "中文流程" in raw and "\\u4e2d" not in raw  # 不转义，直接可读
    assert json.loads(raw)[0]["name"] == "中文流程"

    assert main(["flows"]) == 0  # 人类文本路径零回归
    out = capsys.readouterr().out
    assert "中文流程" in out and "已保存流程" in out


# ----------------------------------------------------------------------
# T16：启动步骤卡（CLI 是唯一真相源，GUI 日志窗经 stdout 自动透传）
# ----------------------------------------------------------------------

def test_record_prints_step_card_on_start(home_dir, tmp_path, monkeypatch, capsys):
    """record 启动打录制步骤卡：浏览 / P 键与「框选」按钮 / 落地 / 结束起名。"""
    _write_saved_site(home_dir)
    fake_downloads_home(monkeypatch, tmp_path)
    page = RecordPage(_START, table=_TABLE)
    install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.recorder.record_session", lambda p, s, h: [])
    assert main(["record", "faketest"]) == 1
    out = capsys.readouterr().out
    assert "── 录制步骤 ──" in out
    assert "在弹出的浏览器里正常浏览" in out
    assert "按 P 键" in out and "「框选」按钮" in out
    assert "抓到的数据当场落地" in out
    assert "给流程起名（留空自动命名）" in out


def test_run_prints_step_card_for_flow_and_recipe(home_dir, tmp_path,
                                                  monkeypatch, capsys):
    """run 启动打执行步骤卡：整链与单条 recipe 两条重放路径都打，各一次。"""
    _seed_flow(home_dir, "cardflow")
    fake_downloads_home(monkeypatch, tmp_path)
    page = RunPage(table=_TABLE)  # 流程路径打桩不碰页；recipe 路径要等选择器
    install_browser(monkeypatch, page)
    monkeypatch.setattr("pageplay.runner.run_flow",
                        lambda *a, **k: {"ok": True, "failed_step": None,
                                         "results": []})

    assert main(["run", "cardflow", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "── 执行步骤 ──" in out
    assert "已按流程逐步执行" in out
    assert "被弹登录页时去窗口里过一下验证" in out
    assert "产物路径最后列出" in out
    assert out.count("── 执行步骤 ──") == 1  # 一条命令只打一张卡

    save_recipe(home_dir / "sites" / "faketest", {  # _seed_flow 已建站点目录
        "version": 1, "name": "cardrecipe", "site": "faketest", "url": _START,
        "action": "table", "selector": "#t1", "columns": ["名称"],
    })
    assert main(["run", "cardrecipe"]) == 0
    assert "── 执行步骤 ──" in capsys.readouterr().out  # 单条 recipe 路径同卡
