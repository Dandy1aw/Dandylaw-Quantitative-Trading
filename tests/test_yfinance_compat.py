from __future__ import annotations

import os
from pathlib import Path

from quant_signal.datafeed.yfinance_compat import ensure_curl_ca_bundle


def test_non_ascii_certificate_path_is_copied_to_ascii_cache(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "证书"
    source_dir.mkdir()
    source = source_dir / "cacert.pem"
    source.write_text("test certificate", encoding="ascii")
    cache = tmp_path / "ascii-cache"

    result = ensure_curl_ca_bundle(source=source, cache_dir=cache)

    assert result == cache / "cacert.pem"
    assert result.read_text(encoding="ascii") == "test certificate"
    assert os.environ["SSL_CERT_FILE"] == str(result)


def test_ascii_certificate_path_does_not_need_copy(tmp_path: Path) -> None:
    source = tmp_path / "cacert.pem"
    source.write_text("test certificate", encoding="ascii")

    result = ensure_curl_ca_bundle(source=source, cache_dir=tmp_path / "unused")

    assert result == source
    assert not (tmp_path / "unused").exists()
