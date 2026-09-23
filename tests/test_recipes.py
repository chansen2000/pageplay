"""pageplay.recipes 单测：全部用 tmp_path，不碰真实 HOME。"""

from __future__ import annotations

import json
import logging
import re

import pytest

from pageplay.recipes import (
    REQUIRED_FIELDS,
    list_recipes,
    load_recipe,
    save_recipe,
)

_ISO_SEC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"


def _recipe(**over) -> dict:
    """一份全字段合法的 recipe 底稿，测试按需覆盖。"""
    base = {
        "version": 1,
        "name": "daily-orders",
        "site": "sycm",
        "url": "https://sycm.taobao.com/order/list",
        "action": "download",
        "selector": "table.order-list",
    }
    base.update(over)
    return base


class TestSaveLoad:
    def test_save_then_load_roundtrip(self, tmp_path) -> None:
        site_dir = tmp_path / "sites" / "sycm"  # 不存在，顺带验证 mkdir parents
        recipe = _recipe()
        path = save_recipe(site_dir, recipe)

        assert path == site_dir / "recipes" / "daily-orders.json"
        loaded = load_recipe(site_dir, "daily-orders")
        assert {k: v for k, v in loaded.items() if k != "created_at"} == recipe
        # created_at 自动补：ISO 秒级（无小数秒）
        assert re.fullmatch(_ISO_SEC, loaded["created_at"])

    def test_saved_json_is_unescaped_utf8_with_all_fields(self, tmp_path) -> None:
        recipe = _recipe(name="订单日报")
        path = save_recipe(tmp_path / "sycm", recipe)

        raw = path.read_text(encoding="utf-8")
        assert "订单日报" in raw          # 中文原样出现
        assert "\\u" not in raw           # ensure_ascii=False，无 \uXXXX
        data = json.loads(raw)
        assert set(REQUIRED_FIELDS) <= set(data)
        assert data["selector"] == "table.order-list"

    def test_existing_created_at_is_kept(self, tmp_path) -> None:
        save_recipe(tmp_path / "sycm", _recipe(created_at="2026-01-01T08:00:00"))
        loaded = load_recipe(tmp_path / "sycm", "daily-orders")
        assert loaded["created_at"] == "2026-01-01T08:00:00"  # 已带则保留

    def test_empty_created_at_is_regenerated(self, tmp_path) -> None:
        save_recipe(tmp_path / "sycm", _recipe(created_at=""))
        loaded = load_recipe(tmp_path / "sycm", "daily-orders")
        assert re.fullmatch(_ISO_SEC, loaded["created_at"])  # 空串不算"已带"


class TestValidation:
    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_missing_required_field_rejected(self, tmp_path, field) -> None:
        recipe = _recipe()
        del recipe[field]
        with pytest.raises(ValueError) as ei:
            save_recipe(tmp_path / "sycm", recipe)
        assert field in str(ei.value)  # 消息点名缺的字段
        assert not (tmp_path / "sycm" / "recipes").exists()  # 校验不过不落盘

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_empty_required_field_rejected(self, tmp_path, field) -> None:
        with pytest.raises(ValueError) as ei:
            save_recipe(tmp_path / "sycm", _recipe(**{field: ""}))
        assert field in str(ei.value)

    def test_illegal_action_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError) as ei:
            save_recipe(tmp_path / "sycm", _recipe(action="click"))
        msg = str(ei.value)
        assert "click" in msg and "download" in msg and "table" in msg
        assert not (tmp_path / "sycm" / "recipes").exists()


class TestOverwrite:
    def test_same_name_overwrites_with_new_content_and_created_at(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_recipe(site_dir, _recipe(url="https://old.example.com/a",
                                      created_at="2026-01-01T00:00:00"))
        save_recipe(site_dir, _recipe(url="https://new.example.com/b",
                                      created_at="2026-02-02T00:00:00"))

        loaded = load_recipe(site_dir, "daily-orders")
        assert loaded["url"] == "https://new.example.com/b"
        assert loaded["created_at"] == "2026-02-02T00:00:00"  # 新值生效，不沿用旧戳
        assert len(list_recipes(site_dir)) == 1  # 只有一份，不版本并存

    def test_overwrite_logs_info(self, tmp_path, caplog) -> None:
        site_dir = tmp_path / "sycm"
        save_recipe(site_dir, _recipe())
        with caplog.at_level(logging.INFO, logger="pageplay.recipes"):
            save_recipe(site_dir, _recipe())
        assert "覆盖" in caplog.text


class TestLoadMissing:
    def test_missing_message_has_pick_guidance(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError) as ei:
            load_recipe(tmp_path / "sycm", "nope")
        assert "先 pageplay pick" in str(ei.value)

    def test_missing_message_lists_existing_recipes(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        save_recipe(site_dir, _recipe(name="alpha"))
        save_recipe(site_dir, _recipe(name="beta"))
        with pytest.raises(FileNotFoundError) as ei:
            load_recipe(site_dir, "nope")
        assert "alpha" in str(ei.value) and "beta" in str(ei.value)


class TestList:
    def test_sorted_by_name_and_trimmed_to_four_keys(self, tmp_path) -> None:
        site_dir = tmp_path / "sycm"
        for name in ("zeta", "alpha", "mid"):
            save_recipe(site_dir, _recipe(name=name))

        got = list_recipes(site_dir)
        assert [r["name"] for r in got] == ["alpha", "mid", "zeta"]
        assert set(got[0]) == {"name", "action", "url", "created_at"}  # 裁剪
        assert got[0]["action"] == "download"
        assert got[0]["url"] == _recipe()["url"]

    def test_missing_site_dir_returns_empty(self, tmp_path) -> None:
        assert list_recipes(tmp_path / "nowhere") == []

    def test_site_dir_without_recipes_dir_returns_empty(self, tmp_path) -> None:
        (tmp_path / "sycm").mkdir()
        assert list_recipes(tmp_path / "sycm") == []


class TestNameSafety:
    @pytest.mark.parametrize("bad", ["..", ".", "a/b", "a\\b", "x..y", "../evil", ""])
    def test_unsafe_name_rejected_and_nothing_written(self, tmp_path, bad) -> None:
        with pytest.raises(ValueError):
            save_recipe(tmp_path / "sycm", _recipe(name=bad))
        assert not (tmp_path / "sycm" / "recipes").exists()  # 一个字节都没写出去

    def test_load_unsafe_name_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            load_recipe(tmp_path / "sycm", "../evil")
