"""日志初始化：统一走 logging 标准库，禁止各模块裸 print 起步。"""

from __future__ import annotations

from pathlib import Path


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    """配置全局日志。

    verbose=False → INFO，True → DEBUG；
    log_file 给定时输出同时落文件（可 tail -f 追踪）；
    格式含时间戳 + 级别 + 模块名 + 行号，供业务流追踪。
    """
    raise NotImplementedError
