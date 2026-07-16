# US Briefing Release Hardening Design

## Goal

Make the US briefing safe to deploy in shadow mode and later switch to live without losing staged-exit alerts, activating orphan entry plans, duplicating a report, hanging the scheduler on Windows, or overstating strategy replay quality.

## Chosen architecture

The deterministic report remains the source of all prices and quantities. Report construction produces candidate payloads plus dormant execution plans. Shadow runs persist report snapshots only. Live runs activate execution plans and advance notified discipline state only after the report card is delivered successfully. A report slot is uniquely identified by `(report_kind, as_of)`; changing retrieval timestamps never creates another delivered report for the same slot.

Codex invocation resolves the npm Windows wrapper to the packaged native executable before spawning it. This makes Python's timeout apply to the real process rather than a `.cmd` parent whose child keeps stdout open. The AI guard derives allowed numbers and uppercase terms from both payload keys and values, so explanations such as `50日线上宽度` and `TREND` are accepted while invented facts remain rejected.

## Strategy safeguards

- Check the next earnings date only for the small preliminary candidate set. Candidates inside the configured blackout window are removed rather than replaced with unchecked names.
- Apply a maximum candidate count per configured risk cluster across all lanes. Expand production clusters beyond semiconductors so a candidate card cannot become one hidden factor bet.
- Add `NQ=F`, `ES=F`, and `^VIX` to the 15:30 non-tradable context alongside Korean indices. These symbols inform the narrative only and can never become candidates or execution plans.
- Replace the close-to-next-close screening replay with an event-driven replay: signal after session close, earliest entry on the next session, entry-zone fill rules, conservative stop-before-target handling when both are touched, multi-session holding, transaction costs on both sides, and optional point-in-time Nasdaq membership.

## Failure and retry semantics

- AI timeout or validation rejection leaves the deterministic card intact.
- Notification failure records a failed report but creates no active execution plan and consumes no discipline stage. A retry can deliver the same actions.
- Shadow runs store projected advice in the report payload but never mutate live discipline state.
- A delivered or shadowed `(report_kind, as_of)` slot is idempotent even if account `retrieved_at` changes.
- Asia-context failure is explicit; it does not block the deterministic report.

## Verification and release

Every reproduced defect gets a failing regression test before implementation. Verification includes focused pipeline tests, actual native Codex CLI smoke with the numeric guard, repository-wide pytest and mypy, lock/diff checks, a production-config scheduler dry run, and an execution-aware synthetic replay. After review, fast-forward the clean production worktree to this branch with `delivery_mode: shadow`; do not switch to live until five real US sessions pass the existing release checklist.
