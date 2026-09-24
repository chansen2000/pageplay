"""GUI 壳单测（T12）：build_command 全分支 + 模块 import 干净。

tkinter UI 本身不自动化测试（人验收）；这里只测纯函数 build_command
（op → argv 翻译与参数校验），并保证 import pageplay.gui 零副作用——
不建 Tk root、不起主循环（main() 才起）。
"""

from __future__ import annotations

import re

import pytest

import pageplay.gui
from pageplay.gui import build_command, next_record_name


def test_import_gui_is_side_effect_free():
    """import 不弹窗不起循环：模块可导入且入口是函数。"""
    assert callable(pageplay.gui.main)
    assert callable(pageplay.gui.App)
    assert callable(build_command)


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
