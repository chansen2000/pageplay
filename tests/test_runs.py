"""runs 执行账本测试：追加/读取往返、过滤、limit、坏行容错、PAGEPLAY_HOME 路径。

纯数据层直测（风格同 test_recipes），不碰 CLI。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pageplay.runs import load_runs, record_run


def _entry(detail: str, **overrides) -> dict:
    """一条合法执行记录（detail 作身份标记），overrides 逐字段覆盖。"""
    entry = {"recipe": "my-recipe", "action": "table", "status": "ok",
             "detail": detail, "outputs": ["/tmp/a.csv", "/tmp/a.json"]}
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# record_run / load_runs 往返
# ---------------------------------------------------------------------------

def test_record_then_load_roundtrip_newest_first(tmp_path: Path):
    """追加三条 → 倒序读回（新在前），字段逐项一致。"""
    runs_path = tmp_path / "runs.jsonl"
    for i in ("一", "二", "三"):
        record_run(runs_path, _entry(f"第{i}条"))

    entries = load_runs(runs_path)

    assert [e["detail"] for e in entries] == ["第三条", "第二条", "第一条"]
    assert entries[0]["recipe"] == "my-recipe"
    assert entries[0]["action"] == "table"
    assert entries[0]["status"] == "ok"
    assert entries[0]["outputs"] == ["/tmp/a.csv", "/tmp/a.json"]
    # created_at 自动补（ISO 秒级，无微秒后缀）
    assert len(entries[0]["created_at"]) == 19


def test_record_preserves_given_created_at(tmp_path: Path):
    """entry 已带 created_at 则原样保留，不覆盖。"""
    runs_path = tmp_path / "runs.jsonl"
    record_run(runs_path, _entry("带时间", created_at="2026-09-24T10:00:00"))

    (entry,) = load_runs(runs_path)
    assert entry["created_at"] == "2026-09-24T10:00:00"


def test_load_filters_by_recipe(tmp_path: Path):
    """带 recipe 名：只返回该 recipe 的记录（仍新在前）。"""
    runs_path = tmp_path / "runs.jsonl"
    record_run(runs_path, _entry("甲", recipe="r1"))
    record_run(runs_path, _entry("乙", recipe="r2"))
    record_run(runs_path, _entry("丙", recipe="r1"))

    entries = load_runs(runs_path, recipe="r1")

    assert [e["detail"] for e in entries] == ["丙", "甲"]


def test_load_limit_takes_newest(tmp_path: Path):
    """limit N：取最近 N 条（新在前）；limit<=0 → 空。"""
    runs_path = tmp_path / "runs.jsonl"
    for i in range(5):
        record_run(runs_path, _entry(f"第{i}条"))

    assert [e["detail"] for e in load_runs(runs_path, limit=2)] == \
        ["第4条", "第3条"]
    assert load_runs(runs_path, limit=0) == []
    assert load_runs(runs_path, limit=-1) == []


def test_load_missing_file_returns_empty(tmp_path: Path):
    """账本文件不存在 → 空列表，不抛。"""
    assert load_runs(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# 坏行容错：追加不受坏行影响；读取跳过坏行
# ---------------------------------------------------------------------------

def test_bad_lines_do_not_block_append_or_load(tmp_path: Path):
    """文件里有坏行：record_run 照常追加；load_runs 跳过坏行只回好行。"""
    runs_path = tmp_path / "runs.jsonl"
    record_run(runs_path, _entry("好行一"))
    runs_path.write_text(
        runs_path.read_text(encoding="utf-8")
        + "{这行不是 json\n"          # 非法 JSON
        + '"只是个字符串"\n'           # 合法 JSON 但不是对象
        + "\n",                        # 空行
        encoding="utf-8")

    record_run(runs_path, _entry("好行二"))  # 坏行存在，追加照常

    entries = load_runs(runs_path)
    assert [e["detail"] for e in entries] == ["好行二", "好行一"]


# ---------------------------------------------------------------------------
# 校验：必填字段 / status / outputs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["recipe", "action", "status", "detail",
                                     "outputs"])
def test_record_missing_field_raises(tmp_path: Path, missing: str):
    """必填字段缺失/空白 → ValueError 点名字段名。"""
    entry = _entry("残缺")
    entry.pop(missing)
    with pytest.raises(ValueError, match=missing):
        record_run(tmp_path / "runs.jsonl", entry)


def test_record_blank_field_raises(tmp_path: Path):
    """空白串视为缺失（outputs 除外——空列表合法）。"""
    with pytest.raises(ValueError, match="detail"):
        record_run(tmp_path / "runs.jsonl", _entry("   "))

    record_run(tmp_path / "runs.jsonl", _entry("空产物也合法", outputs=[]))
    assert load_runs(tmp_path / "runs.jsonl")[0]["outputs"] == []


def test_record_bad_status_raises(tmp_path: Path):
    """status 只认 ok/fail。"""
    with pytest.raises(ValueError, match="status"):
        record_run(tmp_path / "runs.jsonl", _entry("x", status="success"))


def test_record_outputs_not_list_raises(tmp_path: Path):
    """outputs 必须是列表。"""
    with pytest.raises(ValueError, match="outputs"):
        record_run(tmp_path / "runs.jsonl", _entry("x", outputs="/tmp/a.csv"))


# ---------------------------------------------------------------------------
# PAGEPLAY_HOME 路径：账本与 sites 根同级
# ---------------------------------------------------------------------------

def test_cli_runs_path_honors_pageplay_home(home_dir: Path, monkeypatch):
    """cli_pick._runs_path = <PAGEPLAY_HOME>/runs.jsonl；未设时 ~/.pageplay。"""
    from pageplay.cli_pick import _runs_path

    assert _runs_path() == home_dir / "runs.jsonl"

    monkeypatch.delenv("PAGEPLAY_HOME")
    assert _runs_path() == Path.home() / ".pageplay" / "runs.jsonl"


def test_record_writes_jsonl_shape(tmp_path: Path):
    """落盘形状：一行一个 JSON 对象、UTF-8、ensure_ascii=False。"""
    runs_path = tmp_path / "nested" / "runs.jsonl"  # 目录不存在逐级创建
    record_run(runs_path, _entry("中文 detail"))
    lines = runs_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["detail"] == "中文 detail"
