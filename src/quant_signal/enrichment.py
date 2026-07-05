"""UZI-Skill (stock-deep-analyzer) 深度分析信息增强：headless 子进程调用，尽力而为。

失败模式（非零退出码/超时/文件缺失/JSON解析失败）一律返回 None，不抛异常——
这是量化信号之外的补充参考层，不能因为外部工具挂了拖累主流程。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import structlog

log = structlog.get_logger()

_FIELDS = ("ticker", "name", "overall_score", "verdict_label", "panel_consensus", "risks")


def _find_synthesis_path(run_py_path: Path, ticker: str) -> Path | None:
    """run.py 内部按两种可能布局 chdir 到 scripts 目录（Hermes 安装 vs repo-root
    布局），.cache 相对那个 cwd 创建，不一定跟 run.py 同级，两种都探测一下。"""
    candidates = [
        run_py_path.parent / "scripts" / ".cache" / ticker / "synthesis.json",
        run_py_path.parent / ".cache" / ticker / "synthesis.json",
    ]
    return next((c for c in candidates if c.exists()), None)


def run_uzi_analysis(
    ticker: str,
    run_py_path: Path,
    python_exe: str = "python",
    depth: str = "lite",
    timeout_seconds: int = 120,
) -> dict[str, object] | None:
    env = {**os.environ, "UZI_CLI_ONLY": "1", "UZI_NO_AUTO_OPEN": "1"}
    try:
        result = subprocess.run(
            [python_exe, str(run_py_path), ticker, "--no-browser", "--depth", depth],
            env=env,
            timeout=timeout_seconds,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("enrichment.timeout", ticker=ticker, timeout=timeout_seconds)
        return None
    except OSError as e:
        log.warning("enrichment.subprocess_error", ticker=ticker, error=str(e))
        return None

    if result.returncode != 0:
        log.warning("enrichment.nonzero_exit", ticker=ticker, code=result.returncode)
        return None

    synthesis_path = _find_synthesis_path(run_py_path, ticker)
    if synthesis_path is None:
        log.warning("enrichment.missing_synthesis", ticker=ticker, run_py=str(run_py_path))
        return None

    try:
        data = json.loads(synthesis_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("enrichment.parse_failed", ticker=ticker, error=str(e))
        return None

    if not all(f in data for f in _FIELDS):
        log.warning("enrichment.missing_fields", ticker=ticker)
        return None

    return {f: data[f] for f in _FIELDS}
