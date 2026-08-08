"""Load yfinance with a libcurl-compatible CA bundle path on Windows."""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

import certifi


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def ensure_curl_ca_bundle(
    *,
    source: Path | None = None,
    cache_dir: Path | None = None,
) -> Path:
    """Return a readable CA path that curl_cffi can pass to Windows libcurl."""
    if source is None:
        for name in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
            configured = os.environ.get(name)
            if configured and Path(configured).is_file():
                return Path(configured)
        source = Path(certifi.where())
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if _is_ascii_path(source):
        return source

    destination_dir = cache_dir or (
        Path(tempfile.gettempdir()) / "quant-signal-certs"
    )
    destination_dir = destination_dir.resolve()
    if not _is_ascii_path(destination_dir):
        raise RuntimeError("curl CA cache path must contain only ASCII characters")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "cacert.pem"
    if not destination.exists() or destination.read_bytes() != source.read_bytes():
        shutil.copyfile(source, destination)
    os.environ["SSL_CERT_FILE"] = str(destination)
    return destination


ensure_curl_ca_bundle()
yf = importlib.import_module("yfinance")

__all__ = ["ensure_curl_ca_bundle", "yf"]
