"""一次性把用户真实持仓写入虚拟持仓台账，避免系统对已持有标的重复发 BUY 信号。

用法：
    uv run python -m quant_signal.seed_holdings --strategy momentum_rotation --tickers MU,DRAM,RAM,MRVL,SNDK,SNXX,SMH,QQQM,SPY
"""

from __future__ import annotations

import argparse

import structlog

from quant_signal.config import load_settings
from quant_signal.ledger import SignalLedger
from quant_signal.logging_setup import setup_logging

log = structlog.get_logger()


def parse_tickers(raw: str) -> list[str]:
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def seed(ledger: SignalLedger, strategy: str, tickers: list[str]) -> list[str]:
    ledger.set_holdings(strategy, tickers)
    return ledger.get_holdings(strategy)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="初始化虚拟持仓")
    parser.add_argument("--strategy", default="momentum_rotation")
    parser.add_argument("--tickers", required=True, help="逗号分隔的 ticker 列表")
    args = parser.parse_args()

    settings = load_settings()
    ledger = SignalLedger(settings.db_path / "signals.db")
    tickers = parse_tickers(args.tickers)
    result = seed(ledger, args.strategy, tickers)
    log.info("seed_holdings.done", strategy=args.strategy, holdings=sorted(result))
    print(f"{args.strategy} 虚拟持仓已设置为: {sorted(result)}")


if __name__ == "__main__":
    main()
