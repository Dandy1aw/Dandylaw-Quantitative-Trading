from __future__ import annotations

import pytest

from quant_signal.datafeed.earnings import YFinanceEarnings


def test_failures_are_suppressed_until_cooldown_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quant_signal.datafeed.earnings as earnings_module

    calls: list[str] = []
    clock = [100.0]

    class _AlwaysFailTicker:
        def __init__(self, symbol: str) -> None:
            calls.append(symbol)

        @property
        def calendar(self) -> dict[str, object]:
            raise RuntimeError("rate limited")

    monkeypatch.setattr(earnings_module.yf, "Ticker", _AlwaysFailTicker)
    source = YFinanceEarnings(
        failure_ttl_seconds=60.0,
        clock=lambda: clock[0],
    )

    assert source.next_dates(["BOOM"]) == {}
    assert source.next_dates(["BOOM"]) == {}
    assert calls == ["BOOM"]

    clock[0] += 61.0
    assert source.next_dates(["BOOM"]) == {}
    assert calls == ["BOOM", "BOOM"]
