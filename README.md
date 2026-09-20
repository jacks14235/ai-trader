# AI Trader

A small, auditable, **paper-only** Alpaca trading pipeline. The broker is hidden behind a narrow
adapter; proposals are strict data, deterministic code is the only authorizer, and execution checks
both environment and file kill switches. Live trading is intentionally rejected by configuration.

## Setup

```bash
uv sync --extra dev
cp .env.example .env
# Set SEC_USER_AGENT to an application name and monitored contact email.
uv run alembic -x database_url=sqlite:///data/paper/trader.db upgrade head
uv run trader status
uv run trader research plan
uv run trader daily-run
```

The daily command claims a unique scheduled window, records state transitions and portfolio state,
builds a bounded research-candidate slate, collects Alpaca market/news data and official SEC evidence,
and writes hashed raw artifacts plus run-scoped database rows. The optional daily reasoning stage has
no broker interface: it emits structured proposals which deterministic code independently evaluates.
Approved normalized orders reach Alpaca paper trading only when `TRADING_ENABLED=true` on a normal
daily run. `TRADING_ENABLED` and `TRADER_REASONING_ENABLED` both default false. Never commit `.env` or
runtime `data/`.

## Commands

`status`, `daily-run`, `paper-canary`, `reconcile`, `halt`, `portfolio`, `runs list`,
`runs show RUN_ID`, and `agents validate`.
`reconcile` reads authoritative paper-broker orders and fills, updates local audit records, and exits
nonzero when it finds an unresolved mismatch. It never submits an order.

## Broad universe, bounded candidate slate

The configured universe is all active, tradable Alpaca U.S. equities and ETFs rather than a static
symbol allowlist. The daily process does not send that entire catalog to a model. It deterministically
combines current holdings, SPY/QQQ benchmarks, Alpaca's most-active-by-volume and
most-active-by-trade-count lists, top gainers and losers, and a small date-stable exploration sample.
Signals for the same symbol are merged and scored, holdings and benchmarks are pinned, and the final
slate is capped at 50 symbols by `config/universe.yaml`.

Preview the exact slate without changing the database:

```bash
uv run trader universe scan
```

The command prints only the bounded slate. A normal `daily-run` additionally preserves the full
normalized asset catalog in `eligible_assets.json`, the scored slate in `candidate_scan.json`, and
hashes both through the run manifest. This is currently shadow-only: discovery does not create a
proposal or order, and options/crypto are outside this first universe implementation.

## Shadow research collection

The candidate slate now feeds a bounded, deterministic research plan. Every candidate receives an
Alpaca market-context question; up to ten priority symbols receive Alpaca company news; and symbols
with an official SEC ticker-to-CIK mapping also receive SEC submissions and company-facts research.
ETFs and other unmapped instruments simply omit the SEC question rather than failing the run.

Set an SEC-compliant identity in `.env` before using this pipeline:

```dotenv
SEC_USER_AGENT="AI Trader your-monitored-email@example.com"
```

Preview the exact questions without persisting evidence:

```bash
uv run trader research plan
```

`daily-run` performs the collection in shadow mode. It retains the exact official SEC ticker map,
its canonical normalized form, every raw provider payload, `research_plan.json`, and
`research_summary.json`. Research rows are scoped to the run, deduplicated by content within that run,
and linked to every question they answered. Network calls, response bytes, item counts, retries,
wall-clock time, and provider spend are capped by `config/research.yaml`; paid sources are currently
disabled with a zero-dollar budget. The collector has no broker-order interface.

## Agent roles and paper proposals

`config/agents.yaml` registers the research compactor, daily trader, event trader, and weekly
strategist. Each role selects a named model profile, an optional model override, reasoning effort,
prompt file, registered context sources, size/time budgets, and immutable permission boundaries.
Model overrides default to `null`, which inherits the installed Codex CLI default.

Validate the complete configuration without invoking a model:

```bash
uv run trader agents validate
```

The daily trader is wired end to end but guarded by a separate environment switch. To test it on the
next unclaimed daily run, set:

```dotenv
TRADER_REASONING_ENABLED=true
```

If today's normal window is already completed, run the same full pipeline under a unique, explicitly
audited paper-test key without deleting or modifying the original run:

```bash
uv run trader daily-run --test-rerun
```

Test reruns use keys beginning with `daily-test:` and record a `TEST_RERUN` event. They still use the
paper broker and the same bounded research, reasoning, and risk policies, but can never submit an
order even when `TRADING_ENABLED=true`.

The app invokes `codex exec` non-interactively using stdin and a JSON output schema. The session is
ephemeral, user/project Codex configuration is ignored, web and shell tools are disabled, approvals
are disabled, and the sandbox is read-only. The exact prompt, resolved role settings, strategy,
context, evidence IDs, schema, provider logs, response, and hashes are retained under the run
directory. `agent_invocations` records the configured model/profile/effort, start and completion
times, explicit book/evaluation scope, input, cached-input, output, reasoning-output and total token
counts, plus any provider-reported monetary cost. Codex CLI currently reports the token breakdown
but no price or billed cost, so those fields explicitly remain unavailable instead of applying API
prices to CLI subscription usage. `trader runs show RUN_ID` exposes each entry and `trader books show
NAME` aggregates usage by model. Every cited evidence ID must belong to that run.
Unsupported symbols or invented evidence fail the run closed. The application builds a complete,
time-pinned risk context using Alpaca's market clock, fresh IEX quotes, rolling daily dollar volume,
broker-authoritative asset metadata, portfolio snapshots, and today's persisted order activity. It
persists every approval or rejection with the effective policy hash before the executor can submit an
order. The reasoning role always retains `can_submit_orders: false`; only deterministic software owns
the paper broker submission method.

## Simulated research-team books

Simulated books can choose a bounded process from `config/pipelines.yaml`. `single_pass` uses one
manager decision. `research_then_adversary` first produces a cited research packet, then a cited
adversarial review, and finally gives both packets to the book manager. The research roles cannot
see portfolio state or submit proposals; only the final manager can propose trades, and those still
pass through deterministic risk checks before the local simulator fills them. `NO_ACTION` is the
explicit default when the evidence does not justify changing the portfolio. It is stored as a
structured abstention with the evidence gap, observable price/evidence/event triggers, and a future
review time or named event. Every contradiction or dissent claim in a consumed packet must receive
an evidence-cited manager disposition: accepted, rejected, or deferred to a concrete trigger.
Book decisions link to their exact evaluation; failed evaluations retain the attempted reasoning for
audit but are excluded from completed-decision comparisons.
The latest completed book abstention is also carried forward as active waiting memory. Python marks
time and price triggers, compares source content hashes to distinguish genuinely new evidence, and
requires a trade that reopens the wait to cite the exact changed condition. Unresolved named events
cannot authorize a reopen.

The initial controlled pair is documented in `docs/initial_book_trial.md`. Both books use the same
strategy bytes and starting cash; only their process profile differs. Open a separate strategy
variant under a new name rather than repurposing either control book.

Book strategy and operating-note paths must remain inside the project. Each evaluation retains its
exact profile, prompts, note, strategy, model settings, evidence hashes, risk configuration, and
simulator assumptions in an immutable experiment phase. Only one evaluation can own a book at a
time. Research plans must match their candidate scan, and the evaluation rejects stale or malformed
valuation quotes and future performance data. If a process is interrupted, the guarded recovery
command is available only after the parent run is already `FAILED` and every started invocation for
that book has been resolved:

```bash
uv run trader books recover-evaluation EVALUATION_ID --reviewer NAME --note "why it is stale"
```

Every completed book evaluation also records two model-free reference points beginning with the
book's starting cash: flat cash with no interest, and one fractional SPY purchase held without
rebalancing. The SPY reference pays the configured spread, slippage, and commission assumptions and
is marked at the quote midpoint. Reference failures are recorded separately and do not become agent
decisions or consume model calls.

Before enabling automatic paper submissions, exercise the same risk and execution boundary with the
canary. The default is a no-submit dry run. A submitting canary requires both the explicit flag and
the environment gate, and immediately cancels an accepted unfilled order:

```bash
uv run trader paper-canary
TRADING_ENABLED=true uv run trader paper-canary --submit --symbol SPY --notional 25
```

The broker clock still controls submission, so a weekend or closed-session canary is rejected with
`OUTSIDE_TRADING_WINDOW`. A canary that fills before cancellation leaves only the configured small
paper position and records the fill through reconciliation.

The event trader and weekly strategist are configured with prompts and permissions but are not yet
invoked. The weekly role is the only role allowed to propose knowledge changes, and it is disabled by
default.

## Durable event scheduling

Dynamic paper runs are stored in the database rather than in a Codex or cron schedule. A source-backed
market event must use an allowed event type and configured symbol before deterministic policy can create
a future run.

The default discovery adapter reads the U.S. Bureau of Economic Analysis official machine-readable
release calendar. Its URL is fixed in code (it cannot be redirected by configuration), timestamps
must be offset-aware, responses are capped at 1 MB, and only the configured release series are
admitted. The current paper policy tracks GDP, Personal Income and Outlays, and U.S. International
Trade releases, mapping those portfolio-wide macro events to the configured SPY/QQQ universe.

The exact BEA response and its hash are retained with each daily run. A successful response is also
written to a hash-verified cache. Transport errors, HTTP 408/429, and HTTP 5xx responses are retried;
after retries, the provider may use only a cache retrieved within the configured age limit. HTTP 4xx,
malformed JSON, unexpected schemas, oversized responses, stale caches, and hash mismatches fail
closed. The provider deduplicates identical upstream dates before assigning stable annual release
identities.

Preview the configured seven-day lookahead window without changing the database, then let the next
daily run persist any events and their policy-approved follow-ups:

```bash
uv run trader events discover
uv run trader daily-run
uv run trader events today
uv run trader schedule list
```

It is normal for preview output to contain zero candidates when none of the selected BEA series falls
inside the current lookahead window. `retrieval_mode` reports `network` or `cache`, and
`source_updated_at` preserves BEA's own feed metadata. If BEA moves a known release, discovery cancels
the old unclaimed wake-up with an audited old/new timestamp record before scheduling its replacement.
Once a worker has claimed a wake-up, discovery refuses to rewrite that history.

The strict local-file adapter remains available for fixtures and manual shadow tests by selecting a
`file` source in `config/dynamic_runs.yaml`. A file feed has this shape:

```json
{
  "source": "manual-shadow-test",
  "events": [
    {
      "event_type": "ECONOMIC_RELEASE",
      "symbols": ["SPY", "QQQ"],
      "scheduled_at": "2026-08-21T08:30:00-04:00",
      "source_event_id": "us-cpi-2026-08-21",
      "confidence": 0.95,
      "evidence": {"url": "https://example.test/verified-event"},
      "announced_at": "2026-08-20T12:00:00-04:00"
    }
  ]
}
```

The daily artifact directory preserves the exact feed bytes, content hash, retrieval mode, registered
and rescheduled event IDs, cancelled and replacement run IDs, confidence filtering, scope filtering,
duplicate counts, and deterministic policy rejections. Event discovery and execution remain
shadow-only and cannot place a trade. The official BEA calendar requires no API key.

Events can also be inserted manually for scheduler testing:

```bash
uv run trader events add \
  --event-type ECONOMIC_RELEASE \
  --symbol SPY \
  --scheduled-at 2026-08-21T08:30:00-04:00 \
  --source manual-test \
  --source-event-id us-cpi-2026-08-21 \
  --evidence-json '{"note":"manual scheduler test"}'

uv run trader schedule create EVENT_ID \
  --scheduled-for 2026-08-21T08:40:00-04:00 \
  --reason "Review the verified release after publication"

uv run trader events list
uv run trader schedule list
uv run trader scheduler-tick
```

Run `scheduler-tick` once per minute from a systemd timer on an always-on server. Each invocation is
stateless: the database atomically claims due work with an expiring lease, rejects duplicates, expires
late jobs, and retains append-only lifecycle events. `trader event-run SCHEDULED_RUN_ID` is available
for an operator-triggered due run and uses the same lease protections. Cancelling an event cancels all
of its unclaimed runs. Event runs currently record `NO_ACTION`; the configured event-trader role will
be wired in a later implementation phase.

## Safety boundary

The risk engine evaluates proposals cumulatively using Decimal arithmetic. The executor accepts only
approved normalized orders, uses deterministic client IDs, persists `SUBMITTING` before the network,
and reconciles timeouts rather than retrying blindly. This is engineering software, not investment
advice. Keep it in paper mode until the complete live-readiness checklist has been manually verified.

Alpaca may force newly created paper accounts to report options level 3 even when level 0 is requested.
The daily runner permits that exact provider-managed paper condition, records it as a safety exception,
and still requires broker-authoritative asset metadata proving every proposed instrument is an active,
tradable U.S. equity from the configured allowlist. Options and crypto remain deterministically blocked.
