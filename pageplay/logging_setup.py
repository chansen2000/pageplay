"""日志初始化：统一走 logging 标准库，禁止各模块裸 print 起步。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    """配置全局日志。

    verbose=False → INFO，True → DEBUG；
    log_file 给定时输出同时落文件（可 tail -f 追踪）；
    格式含时间戳 + 级别 + 模块名 + 行号，供业务流追踪。

    幂等策略：选"清 root handler"方案——每次调用先移除 root logger
    上全部既有 handler 再重新挂载，重复调用不会叠加 handler。不用
    独立 logger 方案，因为 CLI 工具需要第三方库（playwright 等）的
    日志同样汇入本配置，root 是唯一汇聚点。
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
