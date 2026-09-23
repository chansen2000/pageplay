"""风控护栏：动作间随机延时 + 响应文本风控关键词检测 + 夜间宵禁。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class RiskTriggered(RuntimeError):
    """检测到风控信号（滑块/验证码/频率限制等）时抛出。"""


class Guard:
    """动作护栏：wait() 做随机间隔延时，check_response() 扫风控关键词。"""

    def __init__(self, min_delay: float = 1.8, max_delay: float = 3.5,
                 curfew: tuple[int, int] | None = (1, 6),
                 risk_keywords: tuple[str, ...] = (
                     "滑块", "验证码", "操作过于频繁", "请重新登录", "异常请求", "风控",
                 ),
                 bypass_curfew: bool = False) -> None:
        """初始化护栏参数。

        min_delay/max_delay：动作间隔随机延时的下/上界（秒）；
        curfew：宵禁时段 (起时, 止时)（24h 制，左闭右开 [起, 止)），
            None 表示不启用；本地时间命中时段时 wait() 直接 raise；
        risk_keywords：响应文本中的风控关键词表；
        bypass_curfew：True 时跳过宵禁检查（默认 False，仅人工确认
            后使用，不破坏既有签名的新增关键字参数）。
        """
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.curfew = curfew
        self.risk_keywords = tuple(risk_keywords)
        self.bypass_curfew = bypass_curfew

    def wait(self) -> None:
        """动作间隔随机延时：在 [min_delay, max_delay] 内取随机秒数休眠。

        宵禁检查在本方法内进行（不单独开方法）：当前本地小时落在
        curfew [起, 止) 且未 bypass_curfew 时直接 raise RuntimeError，
        不执行延时。sycm 的 "618" 豁免是站点特例，不搬。
        """
        if self.curfew is not None and not self.bypass_curfew:
            start, end = self.curfew
            hour = datetime.now().hour  # 本地时间
            if start <= hour < end:
                raise RuntimeError(
                    f"当前本地时间 {hour} 点处于宵禁时段 [{start}, {end})，"
                    "已暂停动作；如确需执行请以 bypass_curfew=True 构造 Guard"
                )
        delay = random.uniform(self.min_delay, self.max_delay)
        logger.debug("护栏延时 %.3fs", delay)
        time.sleep(delay)

    def check_response(self, text: str) -> None:
        """扫描响应文本：命中任一 risk_keywords 则 raise RiskTriggered。"""
        for keyword in self.risk_keywords:
            if keyword in text:
                logger.warning("响应命中风控关键词：%r", keyword)
                raise RiskTriggered(f"响应命中风控关键词：{keyword}")
