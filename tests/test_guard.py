"""guard 单测：延时区间、风控关键词、宵禁边界。全程打桩不真 sleep。"""

from __future__ import annotations

from datetime import datetime

import pytest

import pageplay.guard as guard_mod
from pageplay.guard import Guard, RiskTriggered


class _FixedHour:
    """datetime 替身：now() 恒返回指定本地小时，供宵禁测试打桩。"""

    def __init__(self, hour: int) -> None:
        self._hour = hour

    def now(self) -> datetime:
        return datetime(2026, 9, 23, self._hour, 0, 0)


@pytest.fixture
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把 guard 里的 time.sleep 换成参数记录器，绝不真睡。"""
    calls: list[float] = []
    monkeypatch.setattr(guard_mod.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _patch_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    monkeypatch.setattr(guard_mod, "datetime", _FixedHour(hour))


# ---------- wait()：随机延时区间 ----------

def test_wait_delay_within_default_interval(sleep_calls: list[float]) -> None:
    guard = Guard()
    for _ in range(50):
        guard.wait()
    assert len(sleep_calls) == 50
    assert all(1.8 <= seconds <= 3.5 for seconds in sleep_calls)
    # 随机性抽查：50 次取值应张开一定宽度，防实现退化成固定值
    assert max(sleep_calls) - min(sleep_calls) > 0.5


def test_wait_delay_within_custom_interval(sleep_calls: list[float]) -> None:
    guard = Guard(min_delay=0.5, max_delay=1.0)
    guard.wait()
    assert len(sleep_calls) == 1
    assert 0.5 <= sleep_calls[0] <= 1.0


# ---------- wait()：宵禁 curfew=(1,6) 左闭右开 ----------

@pytest.mark.parametrize(("hour", "blocked"), [(0, False), (1, True), (5, True), (6, False)])
def test_curfew_boundaries(sleep_calls: list[float], monkeypatch: pytest.MonkeyPatch,
                           hour: int, blocked: bool) -> None:
    _patch_hour(monkeypatch, hour)
    guard = Guard()
    before = len(sleep_calls)
    if blocked:
        with pytest.raises(RuntimeError, match="宵禁"):
            guard.wait()
        assert len(sleep_calls) == before  # 宵禁期不执行延时
    else:
        guard.wait()
        assert len(sleep_calls) == before + 1


def test_curfew_bypass_allows_wait(sleep_calls: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_hour(monkeypatch, 3)  # 宵禁正中间
    guard = Guard(bypass_curfew=True)
    guard.wait()  # 不抛即通过
    assert len(sleep_calls) == 1


def test_curfew_none_disables_check(sleep_calls: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_hour(monkeypatch, 3)
    guard = Guard(curfew=None)
    guard.wait()
    assert len(sleep_calls) == 1


# ---------- check_response()：风控关键词 ----------

@pytest.mark.parametrize(("text", "keyword"), [
    ("检测到异常滑动，请完成滑块验证", "滑块"),
    ("请输入验证码", "验证码"),
    ("操作过于频繁，请稍后再试", "操作过于频繁"),
    ("登录已失效，请重新登录", "请重新登录"),
    ("检测到异常请求，已拦截", "异常请求"),
    ("系统检测到您的账号存在风控风险", "风控"),
])
def test_check_response_hits_keyword(text: str, keyword: str) -> None:
    guard = Guard()
    with pytest.raises(RiskTriggered, match=keyword):
        guard.check_response(text)


def test_check_response_passes_innocent_text() -> None:
    guard = Guard()
    assert guard.check_response("订单提交成功") is None
    assert guard.check_response("") is None


def test_check_response_uses_custom_keywords() -> None:
    guard = Guard(risk_keywords=("会员到期",))
    with pytest.raises(RiskTriggered, match="会员到期"):
        guard.check_response("您的会员到期了")
    assert guard.check_response("默认关键词不再生效：滑块") is None


def test_risk_triggered_is_runtime_error() -> None:
    # 契约：RiskTriggered 继承 RuntimeError（cli 据此映射 exit 码 2）
    assert issubclass(RiskTriggered, RuntimeError)
