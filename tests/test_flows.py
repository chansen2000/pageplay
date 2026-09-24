"""pageplay.flows 单测：全部用 tmp_path，不碰真实 HOME。"""

from __future__ import annotations

import json
import logging
import re

import pytest

from pageplay.flows import (
    REQUIRED,
    VALID_KINDS,
    list_flows,
    load_flow,
    next_flow_name,
    save_flow,
)

_ISO_SEC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"


def _flow(**over) -> dict:
    """一份全字段合法的 flow 底稿，测试按需覆盖。"""
    base = {
        "version": 1,
        "name": "sycm-flow-1",
        "site": "sycm",
        "url": "https://sycm.taobao.com",
        "steps": [
            {"kind": "goto", "url": "https://sycm.taobao.com/order/list"},
            {"kind": "table", "selector": "table.order-list"},
        ],
    }
    base.update(over)
    return base


class TestSaveLoad:
    def test_save_then_load_roundtrip(self, tmp_path) -> None:
        site_dir = tmp_path / "sites" / "sycm"  # 不存在，顺带验证 mkdir parents
        flow = _flow()
        path = save_flow(site_dir, flow)

        assert path == site_dir / "flows" / "sycm-flow-1.json"
        loaded = load_flow(site_dir, "sycm-flow-1")
        # created_at 自动补：ISO 秒级（无小数秒）
        assert re.fullmatch(_ISO_SEC, loaded["created_at"])
        assert {k: v for k, v in loaded.items() if k != "created_at"} == {
            k: v for k, v in flow.items() if k != "created_at"
        } | {"steps": [dict(s, no=i) for i, s in enumerate(flow["steps"], 1)]}

    def test_saved_json_is_unescaped_utf8_with_all_fields(self, tmp_path) -> None:
        flow = _flow(name="订单流程")
        path = save_flow(tmp_path / "sycm", flow)

        raw = path.read_text(encoding="utf-8")
        assert "订单流程" in raw          # 中文原样出现
        assert "\\u" not in raw           # ensure_ascii=False，无 \uXXXX
        data = json.loads(raw)
        assert set(REQUIRED) <= set(data)
        assert data["steps"][1]["selector"] == "table.order-list"

    def test_existing_created_at_is_kept(self, tmp_path) -> None:
        save_flow(tmp_path / "sycm", _flow(created_at="2026-01-01T08:00:00"))
        loaded = load_flow(tmp_path / "sycm", "sycm-flow-1")
        assert loaded["created_at"] == "2026-01-01T08:00:00"  # 已带则保留

    def test_empty_created_at_is_regenerated(self, tmp_path) -> None:
        save_flow(tmp_path / "sycm", _flow(created_at=""))
        loaded = load_flow(tmp_path / "sycm", "sycm-flow-1")
        assert re.fullmatch(_ISO_SEC, loaded["created_at"])  # 空串不算"已带"

    def test_step_no_assigned_from_1_and_input_not_mutated(self, tmp_path) -> None:
        steps = [
            {"kind": "goto", "url": "https://a.example.com", "no": 99},
            {"kind": "click", "selector": "#go", "no": 5},
            {"kind": "table", "selector": "table.data"},  # 没带 no
        ]
        save_flow(tmp_path / "sycm", _flow(steps=steps))
        loaded = load_flow(tmp_path / "sycm", "sycm-flow-1")

        assert [s["no"] for s in loaded["steps"]] == [1, 2, 3]  # 旧 no 丢弃重排
        assert [s["kind"] for s in loaded["steps"]] == ["goto", "click", "table"]
        assert steps[0]["no"] == 99  # 落盘的是副本，不改调用方底稿


class TestValidation:
    @pytest.mark.parametrize("field", REQUIRED)
    def test_missing_required_field_rejected(self, tmp_path, field) -> None:
        flow = _flow()
        del flow[field]
        with pytest.raises(ValueError) as ei:
            save_flow(tmp_path / "sycm", flow)
        assert field in str(ei.value)  # 消息点名缺的字段
        assert not (tmp_path / "sycm" / "flows").exists()  # 校验不过不落盘

    @pytest.mark.parametrize("field", REQUIRED)
    def test_empty_required_field_rejected(self, tmp_path, field) -> None:
        with pytest.raises(ValueError) as ei:
            save_flow(tmp_path / "sycm", _flow(**{field: ""}))
        assert field in str(ei.value)

    def test_empty_steps_list_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError) as ei:
            save_flow(tmp_path / "sycm", _flow(steps=[]))
        assert "steps" in str(ei.value)
        assert not (tmp_path / "sycm" / "flows").exists()

    def test_steps_not_a_list_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError) as ei:
            save_flow(tmp_path / "sycm", _flow(steps="goto"))
        assert "steps" in str(ei.value)

    @pytest.mark.parametrize("kind", ["delete", "", 42, None])
    def test_illegal_kind_rejected(self, tmp_path, kind) -> None:
        with pytest.raises(ValueError) as ei:
            save_flow(tmp_path / "sycm",
                      _flow(steps=[{"kind": kind}]))
        msg = str(ei.value)
        assert "kind" in msg
        assert "goto" in msg and "download" in msg  # 消息列出合法 kind
        assert not (tmp_path / "sycm" / "flows").exists()

    def test_step_not_a_dict_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            save_flow(tmp_path / "sycm",
                      _flow(steps=[{"kind": "goto"}, "not-a-dict"]))

    def test_valid_kinds_all_accepted(self, tmp_path) -> None:
        steps = [{"kind": k} for k in VALID_KINDS]
        save_flow(tmp_path / "sycm", _flow(steps=steps))
        loaded = load_flow(tmp_path / "sycm", "sycm-flow-1")
        assert [s["kind"] for s in loaded["steps"]] == list(VALID_KINDS)


class TestOverwrite:
    def test_same_name_overwrites_with_new_content_and_created_at(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_flow(site_dir, _flow(url="https://old.example.com",
                                  created_at="2026-01-01T00:00:00"))
        save_flow(site_dir, _flow(url="https://new.example.com",
                                  created_at="2026-02-02T00:00:00"))

        loaded = load_flow(site_dir, "sycm-flow-1")
        assert loaded["url"] == "https://new.example.com"
        assert loaded["created_at"] == "2026-02-02T00:00:00"  # 新值生效，不沿用旧戳
        assert len(list_flows(site_dir)) == 1  # 只有一份，不版本并存

    def test_overwrite_logs_info(self, tmp_path, caplog) -> None:
        site_dir = tmp_path / "sycm"
        save_flow(site_dir, _flow())
        with caplog.at_level(logging.INFO, logger="pageplay.flows"):
            save_flow(site_dir, _flow())
        assert "覆盖" in caplog.text


class TestLoadMissing:
    def test_missing_message_has_record_guidance(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError) as ei:
            load_flow(tmp_path / "sycm", "nope")
        assert "先 pageplay record" in str(ei.value)

    def test_missing_message_lists_existing_flows(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_flow(site_dir, _flow(name="alpha"))
        save_flow(site_dir, _flow(name="beta"))
        with pytest.raises(FileNotFoundError) as ei:
            load_flow(site_dir, "nope")
        assert "alpha" in str(ei.value) and "beta" in str(ei.value)


class TestList:
    def test_sorted_by_name_and_trimmed_to_four_keys(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        for name in ("zeta", "alpha", "mid"):
            save_flow(site_dir, _flow(name=name))

        got = list_flows(site_dir)
        assert [f["name"] for f in got] == ["alpha", "mid", "zeta"]
        assert set(got[0]) == {"name", "url", "step_count", "created_at"}  # 裁剪
        assert got[0]["url"] == _flow()["url"]
        assert got[0]["step_count"] == 2  # 按 steps 现算

    def test_missing_site_dir_returns_empty(self, tmp_path) -> None:
        assert list_flows(tmp_path / "nowhere") == []

    def test_site_dir_without_flows_dir_returns_empty(self, tmp_path) -> None:
        (tmp_path / "sycm").mkdir()
        assert list_flows(tmp_path / "sycm") == []

    def test_corrupt_or_non_dict_file_skipped_with_warning(
            self, tmp_path, caplog) -> None:
        flows_dir = tmp_path / "sycm" / "flows"
        flows_dir.mkdir(parents=True)
        (flows_dir / "broken.json").write_text("{not json", encoding="utf-8")
        (flows_dir / "alist.json").write_text("[1, 2]", encoding="utf-8")
        save_flow(tmp_path / "sycm", _flow(name="good"))

        with caplog.at_level(logging.WARNING, logger="pageplay.flows"):
            got = list_flows(tmp_path / "sycm")
        assert [f["name"] for f in got] == ["good"]  # 坏文件不炸清单
        assert caplog.text.count("跳过") == 2


class TestNextFlowName:
    def test_no_existing_flows_starts_at_1(self, tmp_path) -> None:
        assert next_flow_name(tmp_path / "sycm", "sycm") == "sycm-flow-1"

    def test_increments_after_existing(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_flow(site_dir, _flow())  # sycm-flow-1
        assert next_flow_name(site_dir, "sycm") == "sycm-flow-2"
        save_flow(site_dir, _flow(name="sycm-flow-2"))
        assert next_flow_name(site_dir, "sycm") == "sycm-flow-3"

    def test_gap_takes_max_plus_one(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_flow(site_dir, _flow(name="sycm-flow-1"))
        save_flow(site_dir, _flow(name="sycm-flow-3"))
        assert next_flow_name(site_dir, "sycm") == "sycm-flow-4"  # 跳号取最大+1

    def test_other_site_prefix_and_non_numeric_tail_ignored(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_flow(site_dir, _flow(name="other-flow-9"))
        save_flow(site_dir, _flow(name="sycm-flow-abc"))
        assert next_flow_name(site_dir, "sycm") == "sycm-flow-1"


class TestNameSafety:
    @pytest.mark.parametrize("bad", ["..", ".", "a/b", "a\\b", "x..y", "../evil", ""])
    def test_unsafe_name_rejected_and_nothing_written(self, tmp_path, bad) -> None:
        with pytest.raises(ValueError):
            save_flow(tmp_path / "sycm", _flow(name=bad))
        assert not (tmp_path / "sycm" / "flows").exists()  # 一个字节都没写出去

    def test_load_unsafe_name_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            load_flow(tmp_path / "sycm", "../evil")
