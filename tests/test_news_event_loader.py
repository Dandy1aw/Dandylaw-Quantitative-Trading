from datetime import datetime, timedelta, timezone
import threading
import time

from quant_signal.datafeed.news import NewsArticle
from quant_signal.news_event_loader import NewsWindow, fetch_news_windows


class SlowSource:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def fetch(self, symbols, start, end):  # type: ignore[no-untyped-def]
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return [NewsArticle(symbols[0], start, start, (symbols[0],), "h", "", "", "", "s")]


def test_event_windows_fetch_concurrently_and_preserve_keys() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows = [NewsWindow(f"T{i}", now + timedelta(days=i), now + timedelta(days=i + 1))
               for i in range(4)]
    source = SlowSource()

    rows, failures = fetch_news_windows(source, windows, max_workers=4)

    assert source.max_active > 1
    assert failures == {}
    assert set(rows) == {window.key for window in windows}
