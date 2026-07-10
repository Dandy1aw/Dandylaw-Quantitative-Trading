from pathlib import Path


def test_supervisor_log_is_rotated_and_old_archives_are_cleaned() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "run-service.ps1"
    ).read_text(encoding="utf-8")

    assert "service-supervisor-*.log" in script
    assert "Move-Item" in script
    assert "AddDays(-14)" in script


def test_production_settings_enable_execution_and_disable_legacy_deviation() -> None:
    import yaml

    settings_path = (
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    )
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    assert data["index_universe"]["enabled"] is True
    assert data["execution_plan"]["enabled"] is True
    assert data["execution_plan"]["account_provider"] == "alpaca_paper"
    assert data["legacy_price_deviation"]["enabled"] is False
