# AGENTS.md

Context for coding agents working in this repository. This file is not a prompt for the in-app
trading roles; those live in `prompts/` and are invoked only through `trader.agent`.

## What this is

A small, auditable **paper-only** Alpaca trading pipeline. Brokers are hidden behind a narrow
adapter. Models emit structured proposals; deterministic Python is the only authorizer. Daily
reasoning reaches paper execution only through the risk engine and only when explicitly enabled.
Live trading is rejected by configuration.

This is engineering software, not investment advice. Keep paper mode until a human has verified a
complete live-readiness checklist.

## Hard invariants

Do not weaken these. If a change would require relaxing one, stop and say so.

- **Paper only.** `Settings` rejects `trader_environment != paper`, `alpaca_paper=false`, and any
  database URL containing `/live/`. The `runs` table checks `mode = 'paper'`.
- **Models cannot trade.** `can_submit_orders` is typed `Literal[False]`. Reasoning modules must
  not import or receive a `Broker`. The executor is the only component that may submit an order.
- **Fail closed.** Invalid account state, stale data, unknown broker/order state, open orders,
  invented evidence IDs, unsupported symbols, oversized provider responses, and hash mismatches
  abort the run. Do not add silent fallbacks or “best effort” network retries that could double-submit.
- **Human-owned risk and knowledge.** Hard limits live in `config/risk.yaml`. Core policy lives in
  `knowledge/portfolio_policy.md` and `knowledge/strategy.md`. No agent may silently rewrite them.
  Only `weekly_strategist` may *propose* knowledge edits, and that role is disabled.
- **Execution is gated, not absent.** `daily-run` already forwards daily-trader proposals to the
  deterministic risk engine, and to the paper executor when `TRADING_ENABLED=true` and `STOP_TRADING`
  is absent. Discovery, research, and event runs remain shadow-only with no broker-order interface.
  `TRADING_ENABLED` and `TRADER_REASONING_ENABLED` both default false, and `--test-rerun` may
  evaluate risk but can never submit.
- **Options, crypto, shorting, margin, leveraged/inverse ETFs, OTC, and penny stocks are blocked.**
  Alpaca paper accounts may report options level 3 even when level 0 was requested. The daily
  runner may record that as a audited paper exception; local equity-only asset checks still apply.
- **Never commit `.env` or runtime `data/`.** Kill switch file is `STOP_TRADING`.

## Layout

| Path | Role |
| --- | --- |
| `src/trader/` | Application package |
| `config/` | Human-owned YAML policy (risk, universe, research, agents, events) |
| `knowledge/` | Versioned investment notes; structured counterparts belong in SQLite |
| `prompts/` | Role prompts consumed by `codex exec`, not by coding agents |
| `templates/` | Human-owned HTML shells; Python fills them after a run |
| `docs/` | Design notes; `docs/research_pipeline_design.md` is the research roadmap |
| `tests/unit/` | Pytest suite; pythonpath is `src` |
| `migrations/` | Alembic; apply before any persistent DB use |
| `data/` | Runtime DB, caches, hashed run artifacts (gitignored) |

Package map:

- `cli.py` — Typer entrypoint (`uv run trader …`)
- `settings.py` — pydantic-settings from `.env`
- `broker/` — `Broker` protocol and Alpaca paper adapter
- `universe/` — broad Alpaca catalog → bounded candidate slate (cap 50)
- `research/` — deterministic plan + bounded Alpaca/SEC collection; no execution access
- `scheduling/` — durable event runs in SQLite (not cron/Codex schedules)
- `agent/` — context assembly, Codex CLI boundary, role-agnostic invocation, daily/event runners
- `ledger/` — deterministic decision-quality record: theses, performance, report rows; no broker
- `books/` — simulated strategy variants: own cash/positions/equity; never a broker
- `risk/` — Decimal, cumulative, fail-closed authorization
- `execution/` — submit approved normalized orders; reconcile, never blind-retry
- `persistence/` — SQLAlchemy audit schema; money stored as strings
- `logging/` — JSON audit events with recursive secret redaction

## Daily pipeline

`daily-run` claims a unique Eastern window (`daily:YYYY-MM-DD:15:15:America/New_York`). Duplicates
are refused. `--test-rerun` uses `daily-test:` keys and records `TEST_RERUN` without deleting the
original. Config, strategy, and policy bytes are hashed onto the run.

Typical stages (append-only `run_events`):

1. Verify DB and paper broker configuration
2. Fetch account, positions, open orders; fail if open orders remain
3. Reconcile to broker-authoritative orders/fills (nonzero issues abort)
4. Snapshot portfolio
5. Close active theses whose symbol is no longer held (safe only because open orders were refused)
6. Scan universe → `eligible_assets.json` + `candidate_scan.json`
7. Discover BEA (or file) events and schedule policy-approved follow-ups
8. Collect bounded Alpaca/SEC research (shadow)
9. Optionally invoke daily trader for structured proposals
10. Optionally evaluate every proposal in the risk engine, then submit approved paper orders
11. Optionally evaluate each active simulated book against the same slate and research
 (isolated; a book failure cannot abort the live run and never submits)
12. Record the decision ledger from risk-approved **live** proposals → `ledger_summary.json`
13. Record the performance snapshot for the run
14. Render `daily_update.html` from the daily trader's briefing plus post-trade facts, then write
 `daily_report.md`, its `daily_reports` row, and a SHA-256 `manifest.json`

Candidate slate is **not** the full catalog. It pins holdings and SPY/QQQ, then merges most-active
volume/trades, top gainers/losers, and a date-stable exploration sample. Preview with
`trader universe scan` / `trader research plan` (no DB writes).

Research: every candidate gets `MARKET_CONTEXT`; up to `max_deep_symbols` (default 10, holdings
pinned) also get `COMPANY_NEWS` and, if ticker→CIK mapped, `SEC_FILINGS`. Unmapped ETFs omit SEC
rather than failing. Paid providers are disabled with a $0 budget. Evidence IDs are
`sha256(run_id + NUL + content_hash)` (64 hex chars), run-scoped, and must be cited exactly.

## In-app reasoning

`config/agents.yaml` registers four roles. Permissions: filesystem read-only, web search off,
orders off. Only weekly strategist may set `can_mutate_knowledge`.

| Role | Status |
| --- | --- |
| `research_compactor` | Configured; not invoked yet |
| `daily_trader` | Wired; gated by `TRADER_REASONING_ENABLED` + `automatic_daily_run` |
| `event_trader` | Configured; event runs currently record `NO_ACTION` |
| `weekly_strategist` | Wired; gated by `TRADER_STRATEGIST_ENABLED`; proposes only |

Invocation is `codex exec` with stdin + JSON schema: ephemeral, ignore user/project config, no
web/shell, approvals never, sandbox read-only. Prompt, context, schema, hashes, provider logs, and
token counts are retained under the run directory and `agent_invocations`. Invalid symbols or
invented evidence IDs fail the run.

## Role invocation

`agent/invocation.py` is the only path from a role to a model, and it knows nothing about which
role it is running: identity, context object, and output contract are all arguments. `invoke_role`
resolves and enables the role, bounds the context against `max_context_chars`, writes the request
artifacts, opens a `STARTED` row, calls the provider, parses into the caller's Pydantic model, runs
the caller's `validate`, then runs `on_output` and commits — all inside one failure boundary, so a
rejected output leaves `FAILED` plus a `failure.json` and writes no derived rows.

Every call declares a `WorkflowStep`: `role`, an optional lowercase `step`, an `attempt` (1–5), and
an optional `parent_invocation_id`, which must exist and belong to the same run. Those are real
columns, and `(run_id, role, step, attempt)` is unique, so a workflow reconstructs from
`agent_invocations` alone rather than from the free-text `purpose`. An unnamed step is the role's
only call in the run; invoking one role twice requires naming each step. Artifacts follow the same
identity — `agent/<role>`, `agent/<role>/<step>`, `agent/<role>/<step>__attempt-N` — and a
directory that already exists aborts before anything is written. `workflow_trail` returns a run's
invocations in order and is what `trader runs show` prints under `agents`.

`ShadowDailyReasoningPipeline` is now just the daily composition over this primitive: assemble the
daily context, invoke `daily_trader`, validate against that context, persist proposals.

`DailyDecision` is `NO_ACTION` or `PROPOSE_TRADES` (max 10). Proposals may only `BUY` or `SELL`,
cite admitted evidence, and use a symbol from the slate or current positions. The same object must
include `daily_update`: a beginner-facing briefing (headline, lesson, overview, next-day plan, plus
optional teaching sections, glossary, and charts). After risk and execution, Python stamps that
prose into `templates/daily_update.html` with what was actually approved or submitted. The model
cannot claim a fill it has not seen. If reasoning is disabled, the same template is filled with a
deterministic note that no model wrote the story.

`context_sources` is enforced, not decorative: `verify_context_sources` maps every declared source
to the context fields that satisfy it and aborts when a role declares one its assembler cannot
supply. Adding a source to `config/agents.yaml` therefore requires adding it to that role's map
(`DAILY_CONTEXT_SOURCES`, `WEEKLY_CONTEXT_SOURCES`) and to the context object itself.

The daily trader receives memory: `recent_decisions` (up to five prior completed `daily:`/
`daily-test:` runs with each proposal's risk outcome, order status, and fill) and `open_theses`
(active theses for slate or held symbols). Both are cut off at the run's `as_of`, so a replay can
never see a decision that had not been made. A proposal may cite `thesis_id` only for a thesis in
its own context and only for that thesis's symbol.

## Decision ledger

`ledger/` turns each run into reviewable history. It never invokes a model and never touches a
broker: every row is derived from already-persisted proposals, risk decisions, and account
snapshots. Free-text `invalidation_conditions` are stored for a human reader and never interpreted.

Thesis lifecycle, driven only by proposals deterministic risk actually **approved**:

| Situation | Effect |
| --- | --- |
| `BUY`, no active thesis for the symbol | open a thesis |
| `BUY`, thesis already active | restate it from the new proposal |
| `SELL` to `target_position_pct = 0` | close it |
| any other `SELL` | restate it |
| `SELL`, no active thesis | ignored |
| symbol no longer held, no open orders | close it (stage 5, before any new reasoning) |

Every transition appends a `knowledge_changes` row (`THESIS_OPENED`, `THESIS_UPDATED`,
`THESIS_CLOSED_ON_EXIT`, `THESIS_CLOSED_NO_POSITION`) carrying before/after state and a reason. That
row is also the idempotency guard: recording the ledger twice for one run raises
`PersistenceConflictError` rather than double-writing. Rejected proposals leave no trace at all.

`thesis_evidence` links only to research rows persisted **in the same run**. A cited ID that is not
one is counted as `unlinked_evidence_count`, never forged into a link. `performance_snapshots` store
equity, cash, PnL and return against the prior snapshot, and drawdown against a running peak carried
forward in `raw_json`; re-recording a run with different equity is a conflict, not an update.

## Scoring the ledger

`ledger/performance.py` aggregates a period from records that already exist — no model, no broker,
no clock — so two reviews of the same period agree. `load_weekly_performance` returns the equity
curve, the proposal/approval/rejection counts with a rejection-code tally, order and fill counts,
thesis open/close counts, and one `ThesisOutcome` per thesis alive in the period.

An outcome is realized from broker fills, reached by `thesis → trade_proposals.thesis_id →
broker_orders.proposal_id → fills`. `realized_pnl` is average entry cost against the quantity
actually sold, minus commission, so a partial exit reports only the realized part and leaves the
rest in `open_qty`. It is `null` when nothing was bought — unresolved, not flat. `closure` carries
the `knowledge_changes` type that closed the thesis, which distinguishes a deliberate exit from a
position that simply disappeared.

`starting_equity` is the last snapshot *before* the period, falling back to the first inside it, so
a week is measured against where it began rather than against its own first observation.

`ledger/strategy.py` versions the strategy document by content hash. Every run that receives
strategy bytes records the version it reasoned under and sets `runs.strategy_id`; identical bytes
return the existing row, different bytes supersede rather than overwrite. That is what lets a
review name the policy in effect when a decision was made.

## Weekly strategy review

`agent/weekly_runner.py` accepts **no `Broker`** — the strategist reviews decisions already made and
authorized, so it has nothing to execute. `weekly-run` claims `weekly:YYYY-MM-DD:America/New_York`
keyed on the period end, which `review_period` fixes at the most recent Saturday midnight Eastern,
so a weekend run covers the trading week that just finished rather than a partial one.
`weekly-test:` keys allow an extra audited review. It is gated by `TRADER_STRATEGIST_ENABLED` in
addition to the role's `enabled` flag.

`StrategyRecommendation` is `NO_CHANGE` (with a reason) or `PROPOSE_CHANGE` carrying **one**
anchored edit, so a change can be approved, measured, and reverted on its own. Validation is
deterministic and fails the run: cited run/proposal/thesis IDs must appear in the supplied context;
`current_text` must occur in the strategy document **exactly once**, since an absent anchor is
unapplicable and a repeated one is ambiguous; the anchor may not come from the portfolio policy; and
a period with no decisions at all cannot support a change.

Nothing in a run applies a proposal. The review appends `STRATEGY_CHANGE_PROPOSED` (or
`STRATEGY_REVIEW_NO_CHANGE`) to `knowledge_changes`, and a human resolves it with `trader strategy
approve|reject`, which appends `STRATEGY_CHANGE_APPROVED`/`STRATEGY_CHANGE_REJECTED` keyed to the
proposal row and records the reviewer. Approval re-checks the anchor against the document as it
stands now rather than trusting the proposal, then writes the file; the next run records the result
as a new superseding version.

## Simulated books

A book is a simulated portfolio for one strategy variant. It has its own cash, positions, and
equity curve. Nothing in `books/` may reach a broker: quotes come through a `MarketDataSource`
(quote/asset/clock only), and fills are produced by `books/simulator.py`. The simulator crosses
the spread, fills all-or-nothing, settles at the same instant, and refuses a stale or missing
quote. Those assumptions are recorded on every fill.

State is **derived**, not stored: `load_book_state` replays `simulated_fills` from `starting_cash`.
Authorization is the same `risk.engine.evaluate` used by the live line, against the book's own
account. The live risk policy still binds (`expected_max_equity_usd` is 2500), so starting cash
should stay at or below that ceiling.

Live vs book isolation: `trade_proposals.book_id` and `performance_snapshots.book_id` are `NULL`
for the live line. `load_recent_decisions`, `load_weekly_performance`, and `load_equity_curve`
filter `book_id IS NULL` unless a book is requested. Books get no theses; their memory is their
own prior proposals and simulated fills. The roster is capped at `MAX_ACTIVE_BOOKS = 8` because
each active book costs one `daily_trader` invocation per day (`WorkflowStep` step `book_<name>`).
A book failure is isolated and cannot abort the live run.

Open one with a copy of the strategy document:

```bash
cp knowledge/strategy.md knowledge/books/mean-reversion.md
uv run trader books open mean-reversion --cash 2000 --strategy knowledge/books/mean-reversion.md
```

Spawning a book from a weekly proposal is not implemented. The strategist still edits the one
document; a human opens a book when a variant should earn its own track record.

## Event scheduling

Durable paper runs live in SQLite. Default discovery is the official BEA machine-readable
calendar: URL is fixed in code, responses capped, only configured series admitted (GDP, Personal
Income and Outlays, U.S. International Trade), mapped to SPY/QQQ. Fail closed on 4xx, bad JSON,
stale/mismatched cache. `scheduler-tick` is stateless (systemd once per minute): atomic lease,
reject duplicates, expire late jobs. File-source adapter exists for fixtures.

## Persistence and money

SQLite at `sqlite:///data/paper/trader.db`. Alembic default URL is in-memory so omitting a
production URL cannot mutate a real DB. After migrate, use `create_session_factory(url, create_schema=False)`.

Use `Decimal` in domain code; persist monetary values as strings. Prefer timezone-aware datetimes
(UTC in DB; America/New_York for schedule keys). Pydantic configs are typically `extra="forbid"`
and frozen. Raw provider payloads are immutable hashed files under `data/raw/paper/runs/<run_id>/`.

`knowledge/` markdown is human-readable. Beliefs, theses, strategy revisions, and every knowledge
change belong in SQLite (`theses`, `strategies`, `knowledge_changes`) so history is reconstructable.

Packages must stay importable in any order. `trader/risk/__init__.py` deliberately re-exports
nothing: pulling the runtime in there makes `trader.risk.models` drag in the executor, which imports
the persistence layer that defines `RiskDecision`'s writer. Import leaf modules directly.

## Commands

```bash
uv sync --extra dev
cp .env.example .env   # SEC_USER_AGENT must be app name + monitored email
uv run alembic -x database_url=sqlite:///data/paper/trader.db upgrade head
uv run trader status
uv run trader daily-run
uv run trader daily-run --test-rerun
uv run trader weekly-run [--as-of ISO] [--test-rerun]
uv run trader strategy proposals|show ID|approve ID --reviewer NAME|reject ID --reviewer NAME
uv run trader books list|open NAME --cash N --strategy PATH|show NAME|pause|resume|retire
uv run trader reconcile          # never submits; exit 1 on mismatch
uv run trader halt               # STOP_TRADING + cancel open orders
uv run trader portfolio
uv run trader runs list|show RUN_ID
uv run trader paper-canary --symbol SPY --notional 25 [--submit]
uv run trader universe scan
uv run trader research plan
uv run trader agents validate
uv run trader events discover|today|list|add|cancel
uv run trader schedule list|create|cancel
uv run trader scheduler-tick
uv run trader event-run SCHEDULED_RUN_ID
uv run pytest
uv run ruff check .
uv run mypy src
```

## Coding conventions

- Python ≥3.12, src layout, hatchling, Typer CLI, SQLAlchemy 2.x, Pydantic v2.
- Ruff: `E,F,I,UP,B,SIM`, line length 100. Mypy: strict + `pydantic.mypy`.
- Keep modules execution-free unless they already own that boundary (`execution/`, `broker/`).
- Prefer strict typed configs and validators over ad-hoc parsing. Discriminated YAML sources
  (e.g. BEA vs file) should fail on unknown providers.
- Tests should cover fail-closed paths, idempotency, hash/manifest behavior, and permission
  boundaries—not just happy paths. Network in unit tests should be stubbed.
- Do not add model tools (web, shell, filesystem writes) to the Codex invocation.
- Do not connect shadow proposals to `Executor` without an explicit, reviewed design change.

## Current slice vs next

Implemented: paper broker adapter, audit schema, universe scan, Alpaca+SEC shadow research,
BEA/file event discovery, durable scheduler, the role-agnostic invocation primitive, daily trader
reasoning with enforced context sources and prior-decision memory, the decision-quality ledger with
fill-derived thesis outcomes and content-hashed strategy versions, the weekly strategy review with
its human approval path, simulated strategy books with isolated daily evaluation, risk engine,
paper canary, and daily-run wiring of proposals → risk → executor/reconciler.

Not yet: research-compactor packets, event-trader invocation, web/paid providers, automatic
promotion or demotion of a book, weekly review of book equity curves. See
`docs/research_pipeline_design.md` before expanding research and `docs/remaining_work.md` for the
deployment and launch checklist.

Structural gaps that block multi-agent work, in rough dependency order:

- Research collection is single-pass and fully deterministic; there is no validated object through
  which a model may request follow-up research. This is the last blocker for a compactor that does
  more than summarize, and for an event trader that can ask a question before deciding.
- The event trader still has no context assembler, so `event-run` records `NO_ACTION`. The
  invocation primitive and the context-source verifier are both role-agnostic now, so this needs an
  `EventAgentContext` and an `EVENT_CONTEXT_SOURCES` map rather than new machinery.
- Theses are not linked to the strategy version in effect when they opened; attribution currently
  stops at `runs.strategy_id`, so per-strategy outcome comparison is a join away but unwritten.
- No role reads a *sequence* of weekly reviews, so the system cannot yet notice that it keeps
  proposing the same change, or that an approved change did not produce its predicted effect. The
  evaluation plan a review writes is prose for a human, not a scheduled check.
