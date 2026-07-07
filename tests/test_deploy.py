from pathlib import Path


def test_supervisor_log_is_rotated_and_old_archives_are_cleaned() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "run-service.ps1"
    ).read_text(encoding="utf-8")

    assert "service-supervisor-*.log" in script
    assert "Move-Item" in script
    assert "AddDays(-14)" in script
