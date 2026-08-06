# External Research Benchmark Adapter Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current session. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import and validate worth-buy-stocks canonical backtest artifacts without copying, importing, installing, or executing the external project.

**Architecture:** A standalone read-only module validates the public JSON interoperability contract and semantic hash, then returns a small immutable summary. A module CLI prints only normalized JSON and never mutates production state.

**Tech Stack:** Python 3.11 standard library (`argparse`, `copy`, `dataclasses`, `hashlib`, `json`, `pathlib`), pytest.

---

### Task 1: Artifact validation and normalized summary

**Files:**
- Create: `src/quant_signal/external_benchmark.py`
- Create: `tests/test_external_benchmark.py`

- [ ] **Step 1: Write failing contract tests**

Build a local fixture with versions, model, generated time, config, provenance, warnings, validation and a semantic hash. Assert:

```python
summary = load_worth_buy_stocks_artifact(path)
assert summary.source == "worth-buy-stocks"
assert summary.symbol == "AAPL"
assert summary.validation_status == "supports"
assert "bars" not in summary.to_dict()
```

Add tests for tampered content, missing fields, unsupported validation status, invalid single-symbol config, NaN, and exclusion of `generated_at`/`provenance.db_path` from semantic identity.

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_external_benchmark.py -q`

Expected: import failure because `quant_signal.external_benchmark` does not exist.

- [ ] **Step 3: Implement the read-only adapter**

Create:

```python
@dataclass(frozen=True)
class ExternalBenchmarkSummary:
    source: str
    artifact_version: str
    contract_version: str
    model_version: str
    semantic_hash: str
    generated_at: str
    symbol: str
    start: str
    end: str
    data_source: str
    feed: str
    adjustment: str
    validation_status: str
    warning_count: int

    def to_dict(self) -> dict[str, object]: ...


def worth_buy_semantic_hash(artifact: Mapping[str, object]) -> str: ...


def load_worth_buy_stocks_artifact(path: Path) -> ExternalBenchmarkSummary: ...
```

Use a deep copy, remove the three non-semantic fields defined by the public contract, reject non-finite JSON values, validate nested types and dates, and compare hashes with `hmac.compare_digest`.

- [ ] **Step 4: Run adapter tests**

Run: `pytest tests/test_external_benchmark.py -q`

Expected: all pass.

### Task 2: Read-only command-line entry

**Files:**
- Modify: `src/quant_signal/external_benchmark.py`
- Modify: `tests/test_external_benchmark.py`

- [ ] **Step 1: Write failing CLI tests**

Call `main([str(path)])`, parse stdout as JSON, and assert the normalized summary. Call it with a tampered artifact and assert a non-zero `SystemExit` with an error on stderr.

- [ ] **Step 2: Run the CLI tests and verify failure**

Run: `pytest tests/test_external_benchmark.py -q`

Expected: failure because `main` is missing.

- [ ] **Step 3: Implement CLI**

Use `argparse` with one artifact path. Print `json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)` to stdout. Convert validation exceptions into `parser.error(...)`. Add the standard `if __name__ == "__main__": main()` guard.

- [ ] **Step 4: Run tests and type checking**

Run: `pytest tests/test_external_benchmark.py -q`

Expected: all pass.

Run: `mypy src/quant_signal/external_benchmark.py`

Expected: success with no issues.

### Task 3: Integrated verification

**Files:**
- Modify only if verification reveals a defect in the adapter files.

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run full static typing**

Run: `mypy src/quant_signal`

Expected: success with no issues.
