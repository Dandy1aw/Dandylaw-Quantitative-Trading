from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.config import load_settings  # noqa: E402
from quant_signal.datafeed.alpaca_source import AlpacaSource  # noqa: E402
from quant_signal.datafeed.fundamentals import YFinanceFundamentals  # noqa: E402
from quant_signal.datafeed.yf_source import YFinanceSource  # noqa: E402
from quant_signal.extreme_movers import (  # noqa: E402
    Eligibility,
    MoverDirection,
    rank_movers,
    rank_sectors,
)
from quant_signal.feishu_bot import BotMessage, FeishuBotService  # noqa: E402
from quant_signal.ledger import SignalLedger  # noqa: E402
from quant_signal.pipelines.extreme_movers import run_close, run_premarket  # noqa: E402


class RecordingNotifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True

    def send_text(self, chat_id: str, text: str) -> bool:
        self.cards.append(SimpleNamespace(title=f"text:{chat_id}", body_md=text))
        return True

    def send_text_to(self, receive_id: str, receive_id_type: str, text: str) -> bool:
        del receive_id_type
        return self.send_text(receive_id, text)

    def send_card(self, chat_id: str, card: object) -> bool:
        del chat_id
        return self.send(card)

    def download_image(self, message_id: str, image_key: str) -> bytes:
        raise AssertionError(f"unexpected image request: {message_id}/{image_key}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings()
    notifier = RecordingNotifier()
    ledger = SignalLedger(args.db.resolve())
    engine = SimpleNamespace(
        settings=settings,
        source=AlpacaSource(settings.alpaca_key, settings.alpaca_secret),
        confirmation_source=YFinanceSource(),
        fundamentals_source=YFinanceFundamentals(),
        ledger=ledger,
        notifier=notifier,
    )
    now = datetime.now(timezone.utc)
    close_ok = run_close(engine, now)
    session = ledger.latest_complete_extreme_mover_session()
    if not close_ok or session is None:
        raise RuntimeError("live close calculation did not complete")
    events = ledger.extreme_mover_events(session)
    eligible = [event for event in events if event.eligibility is Eligibility.ELIGIBLE]
    premarket_ok = run_premarket(engine, now)
    window = settings.extreme_movers.default_window
    history = ledger.extreme_mover_events(session, window_sessions=window)
    movers = rank_movers(history, window_sessions=window)
    sectors = rank_sectors(history, window_sessions=window)
    run = ledger.extreme_mover_run(session)
    assert run is not None
    allowed = settings.feishu_bot.allowed_open_ids[0]
    bot = FeishuBotService(ledger, settings, notifier)
    commands = ["异动榜 60", "异动板块 科技"]
    if eligible:
        commands.append(f"异动 {eligible[0].ticker}")
    before_bot = len(notifier.cards)
    for index, command in enumerate(commands):
        bot.handle(BotMessage(
            message_id=f"e2e-movers-{session}-{index}",
            chat_id="e2e-recording-chat",
            chat_type="p2p",
            message_type="text",
            content_json=json.dumps({"text": command}, ensure_ascii=False),
            sender_open_id=allowed,
        ))
    bot_outputs = notifier.cards[before_bot:]
    result = {
        "status": "complete",
        "session": session.isoformat(),
        "close_ok": close_ok,
        "premarket_ok": premarket_ok,
        "event_count": len(events),
        "eligible_count": len(eligible),
        "up_count": sum(event.direction is MoverDirection.UP for event in eligible),
        "down_count": sum(event.direction is MoverDirection.DOWN for event in eligible),
        "stock_rank_count": len(movers),
        "sector_rank_count": len(sectors),
        "universe_count": int(run["universe_count"]),
        "covered_count": int(run["covered_count"]),
        "screened_count": int(run["screened_count"]),
        "confirmed_count": int(run["confirmed_count"]),
        "feed": str(run["feed"]),
        "eligibility_counts": dict(Counter(event.eligibility.value for event in events)),
        "session_finalized": all(event.session == session for event in events),
        "bot_commands": commands,
        "bot_output_count": len(bot_outputs),
        "card_titles": [str(getattr(card, "title", "")) for card in notifier.cards],
        "notifications_sent": 0,
        "data_sources": [
            "alpaca_iex_screen+yfinance_adjusted_confirm"
            if settings.extreme_movers.feed == "hybrid"
            else "alpaca_sip_adjustment_all",
            "yfinance_profiles",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
