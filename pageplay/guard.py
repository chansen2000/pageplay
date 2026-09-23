"""风控护栏：动作间随机延时 + 响应文本风控关键词检测。"""

from __future__ import annotations


class RiskTriggered(RuntimeError):
    """检测到风控信号（滑块/验证码/频率限制等）时抛出。"""


class Guard:
    """动作护栏：wait() 做随机间隔延时，check_response() 扫风控关键词。"""

    def __init__(self, min_delay: float = 1.8, max_delay: float = 3.5,
                 curfew: tuple[int, int] | None = (1, 6),
                 risk_keywords: tuple[str, ...] = (
                     "滑块", "验证码", "操作过于频繁", "请重新登录", "异常请求", "风控",
                 )) -> None:
        """初始化护栏参数。

        min_delay/max_delay：动作间隔随机延时的下/上界（秒）；
        curfew：宵禁时段 (起时, 止时)（24h 制），None 表示不启用；
        risk_keywords：响应文本中的风控关键词表。
        """
        raise NotImplementedError

    def wait(self) -> None:
        """动作间隔随机延时：在 [min_delay, max_delay] 内取随机秒数休眠。"""
        raise NotImplementedError

    def check_response(self, text: str) -> None:
        """扫描响应文本：命中任一 risk_keywords 则 raise RiskTriggered。"""
        raise NotImplementedError
