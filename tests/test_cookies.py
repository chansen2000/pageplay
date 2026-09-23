"""pageplay.cookies 单测：全部用 tmp_path，不碰真实 HOME。"""

from __future__ import annotations

import json
import os
import re

import pytest

from pageplay.cookies import (
    export_json,
    filter_by_domain,
    has_login_state,
    load_snapshot,
    save_snapshot,
)


def _cookies() -> list[dict]:
    return [
        {"name": "_tb_token_", "value": "abc123", "domain": ".taobao.com",
         "path": "/", "expires": 1790000000, "httpOnly": False, "secure": False},
        {"name": "tracknick", "value": "seller", "domain": ".taobao.com", "path": "/"},
        {"name": "other", "value": "x", "domain": ".example.com", "path": "/"},
    ]


class TestHasLoginState:
    def test_all_markers_present(self) -> None:
        """标记齐：该域 cookie 里有全部 check_cookies → True。"""
        assert has_login_state(_cookies(), "taobao.com", ("_tb_token_",)) is True

    def test_one_marker_missing(self) -> None:
        """缺一：任一标记不在场 → False（即使另一个在场）。"""
        assert has_login_state(_cookies(), "taobao.com", ("_tb_token_", "nope")) is False

    def test_marker_scoped_to_domain(self) -> None:
        """标记判定只在过滤后的域内找：other 在 example 域，不算 taobao 的标记。"""
        assert has_login_state(_cookies(), "taobao.com", ("other",)) is False

    def test_empty_markers_with_cookies(self) -> None:
        """空标记 + 该域有 cookie → True。"""
        assert has_login_state(_cookies(), "taobao.com", ()) is True

    def test_empty_markers_no_cookies(self) -> None:
        """空标记 + 该域无 cookie → False。"""
        assert has_login_state(_cookies(), "jd.com", ()) is False


class TestFilterByDomain:
    def test_substring_hit(self) -> None:
        got = filter_by_domain(_cookies(), "taobao.com")
        assert [c["name"] for c in got] == ["_tb_token_", "tracknick"]

    def test_no_hit(self) -> None:
        assert filter_by_domain(_cookies(), "jd.com") == []

    def test_empty_domain_field_tolerated(self) -> None:
        """缺 domain 键 / 空 domain 值：不抛异常；按子串语义不命中即过滤掉。"""
        cookies = [{"name": "a", "value": "1"}, {"name": "b", "value": "2", "domain": ""}]
        assert filter_by_domain(cookies, "taobao.com") == []


class TestSnapshot:
    def test_save_then_load_roundtrip(self, tmp_path) -> None:
        site_dir = tmp_path / "sites" / "taobao"  # 不存在，顺带验证 mkdir parents
        cookies = _cookies()
        path = save_snapshot(site_dir, cookies)

        assert path == site_dir / "state.json"
        assert load_snapshot(site_dir) == cookies

    def test_state_json_schema(self, tmp_path) -> None:
        path = save_snapshot(tmp_path / "taobao", _cookies())
        data = json.loads(path.read_text(encoding="utf-8"))

        assert list(data.keys()) == ["version", "site", "saved_at", "cookies"]
        assert data["version"] == 1
        assert data["site"] == "taobao"
        # saved_at 为 ISO 秒级（无小数秒）
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", data["saved_at"])
        assert data["cookies"] == _cookies()

    @pytest.mark.skipif(os.name != "posix", reason="权限位断言仅 posix（Windows 无 0600 语义）")
    def test_file_mode_0600(self, tmp_path) -> None:
        path = save_snapshot(tmp_path / "sycm", _cookies())
        assert path.stat().st_mode & 0o777 == 0o600

    def test_load_missing_file_message(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError) as ei:
            load_snapshot(tmp_path / "nowhere")
        msg = str(ei.value)
        assert "pageplay login nowhere" in msg


class TestExportJson:
    def test_keeps_only_four_keys_and_unescaped_chinese(self) -> None:
        cookies = [
            {"name": "unick", "value": "卖家店铺", "domain": ".taobao.com",
             "path": "/", "expires": 1790000000, "httpOnly": False},
            {"name": "bare", "value": "x"},  # 缺 domain/expires → 跳过缺键
        ]
        out = export_json(cookies)
        data = json.loads(out)

        assert data[0] == {"name": "unick", "value": "卖家店铺",
                           "domain": ".taobao.com", "expires": 1790000000}
        assert data[1] == {"name": "bare", "value": "x"}
        # 中文 value 不转义：原文直接出现，无 \uXXXX
        assert "卖家店铺" in out
        assert "\\u" not in out
