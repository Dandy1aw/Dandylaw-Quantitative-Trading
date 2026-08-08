from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.config import load_settings
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.cards import report_card
from quant_signal.notifier.feishu import get_notifier
from quant_signal.pipelines.extreme_movers import run_premarket


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-live", action="store_true", required=True)
    args = parser.parse_args()
    if not args.confirm_live:
        raise RuntimeError("live delivery requires --confirm-live")
    settings = load_settings()
    ledger = SignalLedger(settings.db_path / "signals.db")
    notifier = get_notifier(settings)
    account = ledger.latest_observed_account()
    positions = ledger.active_observed_positions(exact_only=True)
    if account is None or len(positions) != 6:
        raise RuntimeError("validated six-position snapshot is not active")
    symbols = [str(row["symbol"]) for row in positions]
    expected = {"AAOI", "GOOGL", "MRVL", "SKHY", "SMH", "MUU"}
    if set(symbols) != expected:
        raise RuntimeError("active portfolio differs from the validated screenshot")
    portfolio_card = report_card(
        "✅ 持仓与实时监控已更新",
        "\n".join([
            f"权益 ${account['equity']}｜现金 ${account['cash']}｜持仓市值 ${account['market_value']}",
            "持仓强制监控: " + ", ".join(sorted(symbols)),
            "机器人新增: 异动榜 / 异动板块 / 异动 <代码> / 监控 / 取消监控 / 重推",
            "非交易日不生成新的行动计划。",
        ]),
    )
    portfolio_sent = notifier.send(portfolio_card)
    engine = SimpleNamespace(settings=settings, ledger=ledger, notifier=notifier)
    movers_sent = run_premarket(engine, datetime.now(UTC), notify=True)
    result = {
        "portfolio_sent": portfolio_sent,
        "movers_sent": movers_sent,
        "portfolio_symbols": sorted(symbols),
        "mover_session": str(ledger.latest_complete_extreme_mover_session()),
        "cards_attempted": 2,
    }
    if not portfolio_sent or not movers_sent:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
