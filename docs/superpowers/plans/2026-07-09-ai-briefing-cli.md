# AI Briefing CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily AI opinion card after the premarket report using Claude Code CLI or Codex CLI.

**Architecture:** Add a small `ai_briefing` module that builds a bounded prompt, invokes a configured CLI provider through `subprocess.run`, and returns optional Markdown. The premarket pipeline calls it after normal report generation and sends a separate report card only when the provider succeeds.

**Tech Stack:** Python 3, pydantic settings, pytest, existing notifier cards, subprocess.

---

## File Structure

- Create `src/quant_signal/ai_briefing.py`: provider config helpers, prompt builder, CLI invocation.
- Modify `src/quant_signal/config.py`: add `AIBriefingSettings`.
- Modify `src/quant_signal/pipelines/premarket.py`: build context and send AI card.
- Modify `src/quant_signal/notifier/cards.py`: add `build_ai_briefing_card`.
- Create `tests/test_ai_briefing.py`: unit tests for prompt/provider/card.
- Modify `tests/test_config.py` and/or existing premarket tests if needed.

### Task 1: Configuration and prompt/provider unit tests

**Files:**
- Create: `tests/test_ai_briefing.py`
- Modify: `src/quant_signal/config.py`
- Create: `src/quant_signal/ai_briefing.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert:

```python
def test_claude_code_cli_uses_print_mode(...)
def test_codex_cli_uses_exec_read_only(...)
def test_cli_returns_none_on_failure(...)
def test_prompt_is_bounded_and_does_not_include_secret_words(...)
```

- [ ] **Step 2: Run tests and confirm fail**

Run: `uv run pytest tests/test_ai_briefing.py -q`

Expected: fail because `quant_signal.ai_briefing` does not exist.

- [ ] **Step 3: Implement minimal module**

Implement:

```python
class AIBriefingContext(BaseModel): ...
def build_ai_briefing_prompt(context, max_chars): ...
def run_ai_briefing(settings, context, runner=subprocess.run) -> str | None: ...
```

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_ai_briefing.py -q`

Expected: pass.

### Task 2: Card rendering and premarket integration

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/pipelines/premarket.py`
- Modify/Create tests for cards and pipeline behavior.

- [ ] **Step 1: Write failing tests**

Add tests that assert:

```python
def test_ai_briefing_card_is_report(...)
def test_premarket_sends_ai_card_when_provider_returns_text(...)
def test_premarket_skips_ai_card_when_provider_fails(...)
```

- [ ] **Step 2: Run focused tests and confirm fail**

Run: `uv run pytest tests/test_ai_briefing.py tests/test_cards.py tests/test_engine.py -q`

Expected: fail because the card/pipeline integration is missing.

- [ ] **Step 3: Implement integration**

Add `build_ai_briefing_card()` and call `run_ai_briefing()` from premarket after normal cards.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_ai_briefing.py tests/test_cards.py tests/test_engine.py -q`

Expected: pass.

### Task 3: Full verification

**Files:**
- All changed files.

- [ ] **Step 1: Run full tests**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run type check**

Run: `uv run mypy src`

Expected: success.

- [ ] **Step 3: Run whitespace check**

Run: `git diff --check`

Expected: no whitespace errors.

## Self-review

- Spec coverage: CLI providers, prompt constraints, failure fallback, card rendering, and premarket integration are covered.
- Placeholder scan: no implementation step relies on undefined “later” work.
- Type consistency: provider names are `claude_code_cli` and `codex_cli` throughout.
