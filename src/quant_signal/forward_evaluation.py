"""Immutable multi-horizon outcome evaluation for published candidates."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import cast

import pandas as pd
import structlog

from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger

log = structlog.get_logger()


def evaluate_candidate_forward_returns(
    ledger: SignalLedger,
    store: BarStore,
    *,
    now: datetime,
    horizons: tuple[int, ...] = (5, 10, 20, 21, 63),
    benchmark: str = "QQQ",
    transaction_cost_bps_per_side: float = 5.0,
) -> int:
    pending = ledger.pending_candidate_forward_evaluations(horizons)
    if not pending:
        return 0

    benchmark = benchmark.strip().upper()
    tickers = sorted({str(row["ticker"]) for row in pending} | {benchmark})
    first_day = min(cast(date, row["as_of"]) for row in pending)
    bars = store.read_daily_bars(
        tickers,
        start=datetime.combine(
            first_day - timedelta(days=10), time.min, tzinfo=timezone.utc
        ),
        end=now.astimezone(timezone.utc),
    )
    benchmark_bars: pd.DataFrame | None = None
    try:
        benchmark_bars = cast(
            pd.DataFrame, bars.xs(benchmark, level="ticker").sort_index()
        )
    except KeyError:
        log.warning("candidate_forward_evaluation.benchmark_missing", benchmark=benchmark)
    inserted = 0
    for row in pending:
        ticker = str(row["ticker"])
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        try:
            entry_price = float(payload.get("price", 0.0))
        except (TypeError, ValueError):
            continue
        if entry_price <= 0:
            continue
        try:
            ticker_bars = bars.xs(ticker, level="ticker").sort_index()
        except KeyError:
            continue
        as_of = cast(date, row["as_of"])
        index = pd.DatetimeIndex(ticker_bars.index)
        sessions = ticker_bars.loc[index.date > as_of]
        horizon = cast(int, row["horizon_sessions"])
        if len(sessions) < horizon:
            continue
        exit_price = float(sessions.iloc[horizon - 1]["close"])
        if exit_price <= 0:
            continue
        window = sessions.iloc[:horizon]
        max_favorable = float(window["high"].max()) / entry_price - 1.0
        max_adverse = float(window["low"].min()) / entry_price - 1.0
        raw_return = exit_price / entry_price - 1.0
        cost_rate = transaction_cost_bps_per_side / 10_000.0
        net_return = (
            exit_price * (1.0 - cost_rate) / (entry_price * (1.0 + cost_rate))
            - 1.0
        )
        benchmark_entry: float | None = None
        benchmark_exit: float | None = None
        benchmark_return: float | None = None
        if benchmark_bars is not None:
            benchmark_index = pd.DatetimeIndex(benchmark_bars.index)
            prior = benchmark_bars.loc[benchmark_index.date <= as_of]
            future = benchmark_bars.loc[benchmark_index.date > as_of]
            if not prior.empty and len(future) >= horizon:
                benchmark_entry = float(prior.iloc[-1]["close"])
                benchmark_exit = float(future.iloc[horizon - 1]["close"])
                if benchmark_entry > 0 and benchmark_exit > 0:
                    benchmark_return = benchmark_exit / benchmark_entry - 1.0
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        if ledger.save_candidate_forward_evaluation(
            report_kind=str(row["report_kind"]),
            as_of=as_of,
            rank=cast(int, row["rank"]),
            ticker=ticker,
            horizon_sessions=horizon,
            entry_price=entry_price,
            exit_price=exit_price,
            return_pct=round(raw_return, 8),
            benchmark_ticker=benchmark,
            benchmark_entry_price=benchmark_entry,
            benchmark_exit_price=benchmark_exit,
            benchmark_return_pct=(
                round(benchmark_return, 8) if benchmark_return is not None else None
            ),
            excess_return_pct=(
                round(raw_return - benchmark_return, 8)
                if benchmark_return is not None
                else None
            ),
            max_favorable_excursion_pct=round(max_favorable, 8),
            max_adverse_excursion_pct=round(max_adverse, 8),
            net_return_pct=round(net_return, 8),
            transaction_cost_bps=transaction_cost_bps_per_side * 2.0,
            rule_version=_optional_text(evidence.get("rule_version")),
            model_version=_optional_text(evidence.get("model_version")),
            data_version=_optional_text(evidence.get("data_version")),
            input_semantic_hash=_optional_text(evidence.get("input_semantic_hash")),
            config_hash=_optional_text(evidence.get("config_hash")),
            universe_hash=_optional_text(evidence.get("universe_hash")),
            point_in_time_status=_point_in_time_status(evidence.get("point_in_time")),
            cost_model=(
                cast(dict[str, object], evidence["cost_model"])
                if isinstance(evidence.get("cost_model"), dict)
                else {
                    "kind": "fixed_round_trip_bps",
                    "transaction_cost_bps": transaction_cost_bps_per_side * 2.0,
                }
            ),
            now=now,
        ):
            inserted += 1
    log.info(
        "candidate_forward_evaluation.completed",
        pending=len(pending),
        inserted=inserted,
        horizons=horizons,
    )
    return inserted


def _optional_text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _point_in_time_status(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    return _optional_text(value.get("status"))
