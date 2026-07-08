from datetime import datetime, timezone

from quant_signal.strategies.base import Direction, dedup_key
from quant_signal.watch_monitor import check_deviations, check_target_hits

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
BANDS = [0.02, 0.05, 0.10, 0.20]


def test_positive_deviation_triggers_buy() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 103.0}, now=NOW, threshold=0.02)
    assert len(signals) == 1
    s = signals[0]
    assert s.ticker == "NVDA" and s.direction is Direction.BUY
    assert s.price == 103.0
    assert s.strategy_id == "price_deviation"
    assert s.ts == NOW


def test_negative_deviation_triggers_sell() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 97.0}, now=NOW, threshold=0.02)
    assert len(signals) == 1 and signals[0].direction is Direction.SELL


def test_within_threshold_no_signal() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 101.0}, now=NOW, threshold=0.02)
    assert signals == []


def test_just_below_threshold_not_triggered() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 101.9}, now=NOW, threshold=0.02)
    assert signals == []


def test_missing_live_price_skipped() -> None:
    signals = check_deviations({"NVDA": 100.0, "AMD": 50.0}, {"NVDA": 110.0}, now=NOW, threshold=0.02)
    assert len(signals) == 1 and signals[0].ticker == "NVDA"


def test_multiple_tickers_independent() -> None:
    ref = {"NVDA": 100.0, "AMD": 50.0, "MU": 900.0}
    live = {"NVDA": 103.0, "AMD": 50.1, "MU": 850.0}
    signals = check_deviations(ref, live, now=NOW, threshold=0.02)
    tickers = {s.ticker for s in signals}
    assert tickers == {"NVDA", "MU"}


def test_escalation_bands_give_distinct_dedup_keys() -> None:
    """升到更高档=不同去重键=会各推一次(升级告警)。"""
    s3 = check_deviations({"NVDA": 100.0}, {"NVDA": 103.0}, now=NOW, bands=BANDS)[0]   # +3% -> 2%档
    s7 = check_deviations({"NVDA": 100.0}, {"NVDA": 107.0}, now=NOW, bands=BANDS)[0]   # +7% -> 5%档
    s12 = check_deviations({"NVDA": 100.0}, {"NVDA": 112.0}, now=NOW, bands=BANDS)[0]  # +12% -> 10%档
    keys = {dedup_key(s3), dedup_key(s7), dedup_key(s12)}
    assert len(keys) == 3
    assert "5%" in s7.reason   # 原因里标出所破档位


def test_same_band_same_dedup_key() -> None:
    """同一档内(4%和3%都在2%档)去重键相同，窗口内仍压制，不重复推。"""
    a = check_deviations({"NVDA": 100.0}, {"NVDA": 103.0}, now=NOW, bands=BANDS)[0]  # +3%
    b = check_deviations({"NVDA": 100.0}, {"NVDA": 104.0}, now=NOW, bands=BANDS)[0]  # +4% 仍2%档
    assert dedup_key(a) == dedup_key(b)


def test_target_hit_when_live_reaches_target() -> None:
    signals = check_target_hits({"MU": 95.0}, {"MU": 94.8}, now=NOW)
    assert len(signals) == 1
    s = signals[0]
    assert s.strategy_id == "target_hit" and s.direction is Direction.BUY
    assert "95.00" in s.reason and s.price == 94.8


def test_target_hit_not_fired_above_tolerance() -> None:
    assert check_target_hits({"MU": 95.0}, {"MU": 96.0}, now=NOW) == []
    assert check_target_hits({"MU": 95.0}, {}, now=NOW) == []          # 无实时价跳过


def test_target_hit_within_tolerance_band() -> None:
    # 容差 0.2%: 95×1.002=95.19, 现价 95.1 视为到价
    assert len(check_target_hits({"MU": 95.0}, {"MU": 95.1}, now=NOW)) == 1


def test_bands_direction_independent() -> None:
    """涨破档与跌破档方向不同，键天然不同。"""
    up = check_deviations({"NVDA": 100.0}, {"NVDA": 106.0}, now=NOW, bands=BANDS)[0]
    dn = check_deviations({"NVDA": 100.0}, {"NVDA": 94.0}, now=NOW, bands=BANDS)[0]
    assert up.direction is Direction.BUY and dn.direction is Direction.SELL
    assert dedup_key(up) != dedup_key(dn)
