# Expanded Technology Sector Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand semiconductor and technology recommendations to Top10, keep other groups at Top3, show up to five groups, and send one real Feishu test card with live data.

**Architecture:** Add a deterministic profile-to-recommendation-group classifier and configurable per-group caps to the existing company-profile selector. Broaden only the pre-selection technical pool, keep execution risk controls unchanged, and batch AI research so larger candidate sets remain bounded.

**Tech Stack:** Python 3.12, dataclasses, Pydantic, SQLite, pytest, mypy, Feishu app notifier.

---

### Task 1: Recommendation groups and limits

**Files:**
- Modify: `src/quant_signal/company_profiles.py`
- Modify: `src/quant_signal/config.py`
- Test: `tests/test_company_profiles.py`

- [ ] Add a failing test with 12 semiconductor, 12 non-semiconductor technology, and 5 consumer candidates; require semiconductor Top10, technology Top10, consumer Top3, stable ranks, and at most five groups.
- [ ] Run `uv run pytest -q tests/test_company_profiles.py` and confirm the current uniform Top3 selector fails.
- [ ] Implement `recommendation_group()` and a `top_n_overrides` argument while retaining the default Top3 behavior.
- [ ] Add `candidate_group_top_n_overrides`, set `Semiconductors: 10`, `Technology: 10`, and change `max_sectors` to 5 in configuration.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Pipeline and AI batching

**Files:**
- Modify: `src/quant_signal/pipelines/us_briefing.py`
- Modify: `config/settings.yaml`
- Test: `tests/test_us_briefing_pipeline.py`

- [ ] Add a failing pipeline test proving the wide technical pool does not cut semiconductor candidates at the old cluster cap of two.
- [ ] Add a failing test proving 18 final candidates are split into bounded AI batches and all valid research blocks are merged.
- [ ] Clone discovery settings with `top_n_per_lane=10` and `max_candidates_per_cluster=10` only when the sector-quality filter is enabled.
- [ ] Pass per-group limits into the selector, add `candidate_group` to candidate payloads, and batch company AI contexts in groups of eight.
- [ ] Re-run pipeline and AI tests.

### Task 3: Card grouping and production test send

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`
- Test: `tests/test_cards.py`

- [ ] Add a failing card test requiring `Semiconductors` and `Technology` sections and preserving complete price/risk text.
- [ ] Render by `candidate_group`, while retaining GICS and detailed industry facts in payloads.
- [ ] Run card tests, full `uv run pytest -q`, `uv run mypy src`, and `git diff --check`.
- [ ] Run an isolated real-data daily-action dry-run with a capture notifier and verify caps, market-cap threshold, AI output, and no option-flow execution.
- [ ] Restart the supervised `quant-signal` process and verify the new PID and scheduler startup log.
- [ ] Build one live-data card titled with `测试` and send it once through the configured Feishu app notifier; record the boolean result and ensure no option-flow slot is created.
