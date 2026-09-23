"""logging_setup 单测：级别切换、文件落地、幂等（handler 不叠加）。"""

from __future__ import annotations

import logging
import re

import pytest

from pageplay.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def _restore_root_logging() -> None:
    """每条用例后恢复 root handler 与级别，不污染同进程其他测试。"""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    yield
    root.handlers[:] = handlers_before
    root.setLevel(level_before)


def _root() -> logging.Logger:
    return logging.getLogger()


def _stderr_handler_count(handlers: list) -> int:
    """FileHandler 是 StreamHandler 子类，数 stderr handler 时要排除。"""
    return sum(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in handlers
    )


def _file_handler_count(handlers: list) -> int:
    return sum(isinstance(h, logging.FileHandler) for h in handlers)


# ---------- 级别 ----------

def test_default_level_is_info() -> None:
    setup_logging()
    assert _root().level == logging.INFO


def test_verbose_level_is_debug() -> None:
    setup_logging(verbose=True)
    assert _root().level == logging.DEBUG


def test_level_can_switch_back() -> None:
    setup_logging(verbose=True)
    setup_logging(verbose=False)
    assert _root().level == logging.INFO


# ---------- 文件落地 ----------

def test_log_file_receives_content(tmp_path) -> None:
    log_file = tmp_path / "logs" / "pageplay.log"  # 顺带覆盖父目录自动创建
    setup_logging(log_file=log_file)
    logging.getLogger("pageplay.demo").info("business-flow-marker")
    content = log_file.read_text(encoding="utf-8")
    assert "business-flow-marker" in content
    # 格式契约：时间 + 模块名 + 行号
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", content)
    assert re.search(r"pageplay\.demo:\d+", content)


def test_handler_composition_with_file(tmp_path) -> None:
    setup_logging(log_file=tmp_path / "a.log")
    handlers = _root().handlers
    assert _stderr_handler_count(handlers) == 1
    assert _file_handler_count(handlers) == 1


def test_handler_composition_without_file() -> None:
    setup_logging()
    handlers = _root().handlers
    assert _stderr_handler_count(handlers) == 1
    assert _file_handler_count(handlers) == 0


# ---------- 幂等 ----------

def test_idempotent_no_handler_duplication(tmp_path) -> None:
    root = _root()
    setup_logging()
    count_stderr_only = len(root.handlers)
    assert count_stderr_only >= 1

    setup_logging(log_file=tmp_path / "b.log")
    count_with_file = len(root.handlers)

    setup_logging(log_file=tmp_path / "b.log")
    assert len(root.handlers) == count_with_file  # 重复调用不翻倍

    setup_logging()
    assert len(root.handlers) == count_stderr_only
