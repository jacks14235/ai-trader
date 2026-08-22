# AGENTS.md

Context for coding agents working in this repository. This file is not a prompt for the in-app
trading roles; those live in `prompts/` and are invoked only through `trader.agent`.

## What this is

A small, auditable **paper-only** Alpaca trading pipeline. Brokers are hidden behind a narrow
adapter. Models emit structured proposals; deterministic Python is the only authorizer. Execution
is not wired to daily reasoning yet. Live trading is rejected by configuration.

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
- **Shadow until explicitly connected.** Discovery, research, daily reasoning, and event runs persist
  artifacts and proposals for evaluation. They have no broker-order interface. `TRADING_ENABLED`
  and `TRADER_REASONING_ENABLED` both default false.
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
- `agent/` — context assembly, Codex CLI boundary, daily/event runners
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
5. Scan universe → `eligible_assets.json` + `candidate_scan.json`
6. Discover BEA (or file) events and schedule policy-approved follow-ups
7. Collect bounded Alpaca/SEC research (shadow)
8. Optionally invoke daily trader (shadow proposals only)
9. Write `daily_report.md` and a SHA-256 `manifest.json`

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
| `weekly_strategist` | Disabled |

Invocation is `codex exec` with stdin + JSON schema: ephemeral, ignore user/project config, no
web/shell, approvals never, sandbox read-only. Prompt, context, schema, hashes, provider logs, and
token counts are retained under the run directory and `agent_invocations`. Invalid symbols or
invented evidence IDs fail the run.

`DailyDecision` is `NO_ACTION` or `PROPOSE_TRADES` (max 10). Shadow proposals may only `BUY` or
`SELL`, cite admitted evidence, and use a symbol from the slate or current positions.

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

## Commands

```bash
uv sync --extra dev
cp .env.example .env   # SEC_USER_AGENT must be app name + monitored email
uv run alembic -x database_url=sqlite:///data/paper/trader.db upgrade head
uv run trader status
uv run trader daily-run
uv run trader daily-run --test-rerun
uv run trader reconcile          # never submits; exit 1 on mismatch
uv run trader halt               # STOP_TRADING + cancel open orders
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
BEA/file event discovery, durable scheduler, daily trader shadow reasoning, risk engine,
executor/reconciler (unused by daily-run).

Not yet: research-compactor packets, event-trader invocation, weekly strategist, web/paid
providers, wiring proposals → risk → broker. See `docs/research_pipeline_design.md` before
expanding research.
