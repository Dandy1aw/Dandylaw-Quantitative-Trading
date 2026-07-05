import json
import subprocess
from pathlib import Path

import pytest

from quant_signal.enrichment import run_uzi_analysis


def _fake_run_py(tmp_path: Path) -> Path:
    """伪造一个 run.py 路径，用于推导 .cache/{ticker}/synthesis.json 的位置。"""
    run_py = tmp_path / "skills" / "deep-analysis" / "run.py"
    run_py.parent.mkdir(parents=True, exist_ok=True)
    run_py.write_text("# fake", encoding="utf-8")
    return run_py


def _write_synthesis(run_py: Path, ticker: str, data: dict) -> None:
    cache_dir = run_py.parent / ".cache" / ticker
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "synthesis.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_successful_run_parses_synthesis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_py = _fake_run_py(tmp_path)
    _write_synthesis(
        run_py, "MU",
        {
            "ticker": "MU", "name": "Micron Technology, Inc.", "overall_score": 49.8,
            "verdict_label": "谨慎 · 1派看多/6派看空", "panel_consensus": 31.9,
            "risks": ["ROE 极低", "行业景气度承压"],
        },
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0),
    )
    result = run_uzi_analysis("MU", run_py, "python", "lite", 120)
    assert result is not None
    assert result["overall_score"] == 49.8
    assert result["verdict_label"] == "谨慎 · 1派看多/6派看空"
    assert result["risks"] == ["ROE 极低", "行业景气度承压"]


def test_nonzero_exit_code_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_py = _fake_run_py(tmp_path)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=1),
    )
    assert run_uzi_analysis("MU", run_py, "python", "lite", 120) is None


def test_timeout_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_py = _fake_run_py(tmp_path)

    def fake_run(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="run.py", timeout=120)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_uzi_analysis("MU", run_py, "python", "lite", 120) is None


def test_missing_synthesis_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_py = _fake_run_py(tmp_path)   # 不写 synthesis.json
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0),
    )
    assert run_uzi_analysis("MU", run_py, "python", "lite", 120) is None


def test_malformed_json_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_py = _fake_run_py(tmp_path)
    cache_dir = run_py.parent / ".cache" / "MU"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "synthesis.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0),
    )
    assert run_uzi_analysis("MU", run_py, "python", "lite", 120) is None
