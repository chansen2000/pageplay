"""GUI 壳单测（T12）：build_command 全分支 + 预览纯函数 + import 干净。

tkinter UI 本身不自动化测试（人验收）；这里只测纯函数：build_command
（op → argv 翻译与参数校验）、load_csv_for_preview / latest_csv_output
（v0.7-B 数据预览的数据层，T18 起实现在 pageplay.gui_preview、gui.py
回导旧名）、should_preview / _count_runs（v0.7 收口的预览触发闸门），
并保证 import pageplay.gui 零副作用——不建 Tk root、不起主循环
（main() 才起）。T18 追加向导式布局的源码静态断言（防回退成平铺）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import pageplay.gui
import pageplay.gui_preview
from pageplay.gui import (build_command, latest_csv_output,
                          load_csv_for_preview, next_record_name,
                          should_preview)


def test_import_gui_is_side_effect_free():
    """import 不弹窗不起循环：模块可导入且入口是函数。"""
    assert callable(pageplay.gui.main)
    assert callable(pageplay.gui.App)
    assert callable(build_command)
    assert callable(load_csv_for_preview)
    assert callable(latest_csv_output)


# ----------------------------------------------------------------------
# build_command：各 op 正常分支
# ----------------------------------------------------------------------

def test_login_and_site_ops_require_site_or_url():
    """login/doctor/open/export：正常拼 argv；缺站点 → ValueError。"""
    for op in ("login", "doctor", "open", "export"):
        assert build_command(op, {"site_or_url": "taobao"}) == [op, "taobao"]
        assert build_command(op, {"site_or_url": " www.taobao.com "}) \
            == [op, "www.taobao.com"]  # 首尾空白剥掉
        with pytest.raises(ValueError):
            build_command(op)
        with pytest.raises(ValueError):
            build_command(op, {"site_or_url": "   "})


def test_record_passes_name_flag_only_when_present():
    """record：必填站点；名称输入框有值传 --name，无值不传（走默认命名）。"""
    assert build_command("record", {"site_or_url": "taobao"}) == ["record", "taobao"]
    assert build_command("record", {"site_or_url": "taobao",
                                    "name": "my-flow"}) \
        == ["record", "taobao", "--name", "my-flow"]
    with pytest.raises(ValueError):
        build_command("record", {"name": "my-flow"})  # 缺站点


def test_run_takes_dropdown_name_and_optional_headless():
    """run：名字来自下拉（必填）；--headless 仅在勾选时透传。"""
    assert build_command("run", {"name": "myflow"}) == ["run", "myflow"]
    assert build_command("run", {"name": "myflow", "headless": True}) \
        == ["run", "myflow", "--headless"]
    assert build_command("run", {"name": "myflow", "headless": False}) \
        == ["run", "myflow"]
    with pytest.raises(ValueError):
        build_command("run")
    with pytest.raises(ValueError):
        build_command("run", {"site_or_url": "taobao"})  # 只有站点不算名字


def test_paramless_ops_produce_bare_argv():
    """results/flows/recipes/shutdown 不需要参数，直接单 token argv。"""
    for op in ("results", "flows", "recipes", "shutdown"):
        assert build_command(op) == [op]
        assert build_command(op, {"site_or_url": "taobao"}) == [op]  # 多余键忽略


def test_illegal_op_raises():
    """非法 op → ValueError（不静默拼出错误命令）。"""
    with pytest.raises(ValueError):
        build_command("pick")  # 框选是交互命令，GUI 不提供
    with pytest.raises(ValueError):
        build_command("")
    with pytest.raises(ValueError):
        build_command("rm -rf")  # 任何非白名单串都不通过


# ----------------------------------------------------------------------
# grab（T14）：站点可选
# ----------------------------------------------------------------------

def test_grab_site_is_optional():
    """grab：给了站点才传（仅命名/落账用），不给就是裸 grab。"""
    assert build_command("grab") == ["grab"]
    assert build_command("grab", {"site_or_url": "taobao"}) == ["grab", "taobao"]
    assert build_command("grab", {"site_or_url": " www.taobao.com "}) \
        == ["grab", "www.taobao.com"]  # 首尾空白剥掉
    assert build_command("grab", {"name": "x", "headless": True}) == ["grab"]


# ----------------------------------------------------------------------
# next_record_name（T13.3）：录制缺名自动起名
# ----------------------------------------------------------------------

def test_next_record_name_takes_max_known_plus_one():
    """已知流程有同前缀：N = 最大号 + 1（taobao-flow-3 → taobao-flow-4）。"""
    assert next_record_name(
        "taobao", ["taobao-flow-1", "other-flow-9", "taobao-flow-3"]) \
        == "taobao-flow-4"
    # 站点输入带协议/路径/端口也能推出标签
    assert next_record_name(
        "https://www.taobao.com/list", ["taobao-flow-7"]) == "taobao-flow-8"


def test_next_record_name_falls_back_to_timestamp():
    """已知清单没有同前缀（取不到 N）→ <站>-flow-<时间戳后4位> 兜底。"""
    assert re.fullmatch(r"taobao-flow-\d{4}",
                        next_record_name("www.taobao.com", []))
    assert re.fullmatch(r"sycm-flow-\d{4}",
                        next_record_name("sycm", ["taobao-flow-9"]))


def test_next_record_name_empty_site_returns_empty():
    """站点推不出标签（空白）→ 空串：交 build_command 按缺站点报错。"""
    assert next_record_name("   ", ["taobao-flow-1"]) == ""
    assert next_record_name("", []) == ""


# ----------------------------------------------------------------------
# load_csv_for_preview（v0.7-B）：CSV → 预览数据
# ----------------------------------------------------------------------

def _write_csv(tmp_path, text, name="t.csv"):
    """落一份 CSV（utf-8-sig，save_table 产物同款带 BOM）返回路径。"""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8-sig")
    return p


def test_load_csv_preview_chinese_columns_and_rows(tmp_path):
    """中文 CSV：表头与行列一致，值全为 str。"""
    p = _write_csv(tmp_path, "商品,价格\n手机,3999\n水杯,25\n")
    columns, rows = load_csv_for_preview(str(p))
    assert columns == ["商品", "价格"]
    assert rows == [["手机", "3999"], ["水杯", "25"]]
    assert all(isinstance(v, str) for row in rows for v in row)


def test_load_csv_preview_keeps_text_and_empty_cells(tmp_path):
    """dtype=str："001" 不变形为 "1"；空单元格是空串不是 NaN。"""
    p = _write_csv(tmp_path, "编号,备注\n001,x\n002,\n")
    _, rows = load_csv_for_preview(str(p))
    assert rows == [["001", "x"], ["002", ""]]


def test_load_csv_preview_empty_file_raises(tmp_path):
    """空文件 → ValueError（人话，不抛 pandas 原生 EmptyDataError）。"""
    p = _write_csv(tmp_path, "")
    with pytest.raises(ValueError, match="空"):
        load_csv_for_preview(str(p))


def test_load_csv_preview_missing_file_raises(tmp_path):
    """文件不存在 → ValueError（人话）。"""
    with pytest.raises(ValueError, match="找不到"):
        load_csv_for_preview(str(tmp_path / "nope.csv"))


def test_load_csv_preview_truncates_but_columns_complete(tmp_path):
    """600 行：默认截到前 500 行且表头完整；max_rows+1 能探出截断。"""
    body = "\n".join(f"r{i},{i}" for i in range(600))
    p = _write_csv(tmp_path, f"名,值\n{body}\n")
    columns, rows = load_csv_for_preview(str(p))
    assert columns == ["名", "值"]
    assert len(rows) == 500
    assert rows[0] == ["r0", "0"]
    assert rows[-1] == ["r499", "499"]
    _, probe = load_csv_for_preview(str(p), max_rows=501)
    assert len(probe) == 501  # 多要一行要得到 → 确实被截断（GUI 打标记依据）


# ----------------------------------------------------------------------
# latest_csv_output（v0.7-B）：账本最后一条有效记录的 .csv 产物
# ----------------------------------------------------------------------

def _run_line(**kw) -> str:
    """构造一条合法执行账本 JSON 行（字段同 runs.record_run）。"""
    entry = {"recipe": "r", "action": "table", "status": "ok",
             "detail": "d", "outputs": [],
             "created_at": "2026-09-25T10:00:00"}
    entry.update(kw)
    return json.dumps(entry, ensure_ascii=False)


def test_latest_csv_output_ok_with_csv(tmp_path):
    """最后一条 ok 且 outputs 有 .csv → 返回该路径。"""
    p = tmp_path / "runs.jsonl"
    p.write_text(_run_line(outputs=["/x/a.json", "/x/a.csv"]) + "\n",
                 encoding="utf-8")
    assert latest_csv_output(p) == "/x/a.csv"


def test_latest_csv_output_ok_without_csv(tmp_path):
    """最后一条 ok 但 outputs 无 .csv → None。"""
    p = tmp_path / "runs.jsonl"
    p.write_text(_run_line(outputs=["/x/a.json"]) + "\n", encoding="utf-8")
    assert latest_csv_output(p) is None


def test_latest_csv_output_fail_status(tmp_path):
    """最后一条 fail → 即使 outputs 里有 .csv 也 None。"""
    p = tmp_path / "runs.jsonl"
    p.write_text(_run_line(status="fail", outputs=["/x/a.csv"]) + "\n",
                 encoding="utf-8")
    assert latest_csv_output(p) is None


def test_latest_csv_output_skips_bad_line(tmp_path):
    """坏行跳过：ok+csv 记录后的损坏行不妨碍取到该 csv。"""
    p = tmp_path / "runs.jsonl"
    p.write_text(_run_line(outputs=["/x/a.csv"]) + "\n{坏行不是JSON\n",
                 encoding="utf-8")
    assert latest_csv_output(p) == "/x/a.csv"


def test_latest_csv_output_bad_line_only(tmp_path):
    """整份账本只有坏行 → None（不抛异常）。"""
    p = tmp_path / "runs.jsonl"
    p.write_text("完全不是JSON\n", encoding="utf-8")
    assert latest_csv_output(p) is None


def test_latest_csv_output_empty_and_missing_file(tmp_path):
    """空文件 / 文件不存在 → None。"""
    p = tmp_path / "runs.jsonl"
    p.write_text("", encoding="utf-8")
    assert latest_csv_output(p) is None
    assert latest_csv_output(tmp_path / "nope.jsonl") is None


# ----------------------------------------------------------------------
# 预览触发闸门（v0.7 收口）：账本行数有增长才弹
# ----------------------------------------------------------------------

def test_should_preview_only_when_ledger_grows():
    """预览闸门（纯函数）：账本行数有增长才弹；无增长/回退不弹。"""
    assert should_preview(3, 4) is True    # 新增了一条执行记录
    assert should_preview(0, 1) is True    # 此前无账本 → 首条记录
    assert should_preview(3, 3) is False   # doctor 等无收获：不弹旧预览
    assert should_preview(3, 2) is False   # 账本被清/重建：不弹


def test_count_runs_counts_nonblank_lines(tmp_path):
    """账本行数 = 非空行数（尾部空行不计）；文件缺失/读不了 → 0。"""
    p = tmp_path / "runs.jsonl"
    assert pageplay.gui._count_runs(p) == 0  # 文件不存在
    p.write_text(_run_line() + "\n" + _run_line(status="fail") + "\n\n",
                 encoding="utf-8")
    assert pageplay.gui._count_runs(p) == 2


# ----------------------------------------------------------------------
# T18 向导式布局：源码静态断言（防回退成平铺）+ 预览拆分回导
# ----------------------------------------------------------------------

def test_gui_source_keeps_wizard_layout_markers():
    """gui.py 源码含向导四段标记与序号按钮：布局重构的防回退锚点。"""
    source = Path(pageplay.gui.__file__).read_text(encoding="utf-8")
    for marker in ("第 1 步", "第 2 步", "第 3 步", "管理（低频）",
                   "① 登录", "② 录制", "③ 取当前页", "④ 重放",
                   "2A 临时抓一页", "2B 反复自动抓"):
        assert marker in source, f"GUI 源码缺少向导布局标记：{marker!r}"


def test_preview_code_lives_in_gui_preview_and_reexported():
    """预览实现整体在 gui_preview（T18 拆分）；gui 回导同一对象，
    `pageplay.gui.latest_csv_output` 等旧 import 路径不破。"""
    for name in ("latest_csv_output", "load_csv_for_preview",
                 "should_preview", "_count_runs"):
        func = getattr(pageplay.gui_preview, name)
        assert callable(func), f"gui_preview 缺少 {name}"
        assert getattr(pageplay.gui, name) is func  # 同一对象 = 回导非拷贝
    assert callable(pageplay.gui_preview.open_preview_window)
