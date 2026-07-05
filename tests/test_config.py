from quant_signal.config import load_settings

import pytest


def test_load_settings_from_repo_yaml() -> None:
    s = load_settings()
    assert s.data_source in ("yfinance", "alpaca")  # 用户在 settings.yaml 中可切换
    assert "SPY" in s.universe
    assert s.strategies["momentum_rotation"]["top_n"] == 3
    assert s.notify.dedup_hours == 4


def test_env_credentials_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式置空而非 delenv：load_dotenv 默认不覆盖已存在的环境变量，这样测试
    # 不受本机 config/.env 是否真的填了 webhook 影响，保持隔离。
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    s = load_settings()
    assert s.feishu_webhook == ""
