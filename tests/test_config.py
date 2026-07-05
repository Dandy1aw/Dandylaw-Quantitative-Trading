from quant_signal.config import load_settings

import pytest


def test_load_settings_from_repo_yaml() -> None:
    s = load_settings()
    assert s.data_source == "yfinance"
    assert "SPY" in s.universe
    assert s.strategies["momentum_rotation"]["top_n"] == 3
    assert s.notify.dedup_hours == 4


def test_env_credentials_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    s = load_settings()
    assert s.feishu_webhook == ""
