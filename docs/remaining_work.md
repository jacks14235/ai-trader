# Paper Trader: Remaining Work and Raspberry Pi Handoff

Last updated: 2026-09-20

This document tracks the work remaining after the initial Raspberry Pi deployment. The Pi is reported to be running the trader, but its service definitions and installation procedure are not currently stored in this repository. Some verification items below may already be complete on the Pi; in that case, the remaining task is to record the configuration and evidence so the deployment is reproducible.

## Current state

The application currently has:

- An Alpaca paper-account integration with hard-coded paper-only enforcement.
- Daily candidate scanning, bounded research collection, Codex reasoning, deterministic risk evaluation, and paper order submission.
- Durable run, research, reasoning, risk, order, fill, and reconciliation audit records.
- A database-backed event scheduler and a once-per-minute `scheduler-tick` command.
- A paper canary command for testing the real submission path with a small notional order.
- A `STOP_TRADING` kill switch and an emergency halt command.
- Duplicate-run protection and a test-rerun mode that cannot submit orders.
- A verified simulated-book process-profile slice: typed research/adversary packets,
  book-specific contexts and operating notes, durable experiment phases/evaluations, active waiting
  memory with machine-checked reopen conditions, and deterministic cash/SPY reference curves. See
  [the implementation contract](process_profiles_plan.md) for scope and acceptance checks.
- Local verification covers the full suite, migration round trips and contention, lint, and strict
  source type checking. No broker submission was used for this slice.

The last documented live-account checks were against Alpaca paper only. A weekend canary was correctly rejected as outside the trading window, and a full daily test run completed with `NO_ACTION` and a clean reconciliation.

## P0: Complete before unattended paper trading

### 1. Capture the Raspberry Pi deployment in the repository

- [ ] Add the actual systemd service and timer unit files used on the Pi.
- [ ] Add an installation/update script or exact deployment instructions.
- [ ] Record the service user, repository path, working directory, `uv` path, Python version, and Codex CLI version.
- [ ] Record where the environment file, database, raw artifacts, logs, and `STOP_TRADING` file live.
- [ ] Make the configured timezone explicit. Trading schedules should use `America/New_York` and must continue to work across daylight-saving transitions.
- [ ] Ensure two daily runs or scheduler ticks cannot overlap.
- [ ] Document how code and database migrations are updated without losing state.

Do not commit API keys, Codex credentials, or the populated environment file. Commit an example environment file containing only variable names and safe defaults.

### 2. Verify the exact code and schema deployed on the Pi

From the deployed repository directory:

```bash
git rev-parse HEAD
uv sync --frozen
uv run alembic -x database_url=sqlite:///data/paper/trader.db upgrade head
uv run trader agents validate
uv run trader status
uv run trader reconcile
```

Expected results:

- The deployed commit is recorded.
- Agent configuration validation succeeds.
- The account reports `environment: paper`.
- Reconciliation reports no unexplained orders or fills.
- The database is at the current Alembic head.

### 3. Complete one market-hours submission canary

This is the most important remaining launch test. During regular market hours, submit a deliberately small paper order through the same risk and execution path used by the daily trader:

```bash
TRADING_ENABLED=true uv run trader paper-canary \
  --submit \
  --symbol SPY \
  --notional 25
```

Then verify:

```bash
uv run trader runs list
uv run trader reconcile
uv run trader status
```

Acceptance criteria:

- [ ] The canary run completes.
- [ ] The deterministic risk engine approves or rejects it for an understandable reason.
- [ ] If approved, exactly one paper order is created.
- [ ] The local order, broker order, status events, and fill agree.
- [ ] A second reconciliation is idempotent and reports no issues.
- [ ] The account remains the intended small paper account.

If the canary is rejected, inspect its risk decision before changing policy. Do not bypass the risk engine merely to force a successful order.

### 4. Enable unattended submission deliberately

Only after the market-hours canary succeeds:

- [ ] Set `TRADING_ENABLED=true` in the Pi service's private environment.
- [ ] Confirm `STOP_TRADING` is absent.
- [ ] Restart the applicable service and inspect its effective environment without printing secrets.
- [ ] Run `uv run trader agents validate` and confirm paper execution is enabled.
- [ ] Confirm the daily timer fires once on weekdays at the intended New York time.
- [ ] Keep all Alpaca credentials pointed at the paper environment.

The Codex profiles should remain in `paper_proposal` mode. Codex proposes actions; deterministic code owns approval and paper execution.

### 5. Make failures visible

The application has an audit trail, but unattended operation also needs an operator signal.

- [ ] Retain service logs across restarts or forward them to a durable log destination.
- [ ] Alert when a daily run is `FAILED`, remains `RUNNING` too long, or never starts.
- [ ] Alert on reconciliation issues, untracked remote orders/fills, or ambiguous broker status.
- [ ] Alert when Codex authentication expires or a Codex invocation fails.
- [ ] Alert on low disk space, database errors, clock skew, and persistent network failures.
- [ ] Add a simple health-check command that returns a nonzero status for stale/failed operation.

Email, a private Discord/Slack webhook, or another simple push channel is sufficient for paper mode.

### 6. Back up and restore the audit trail

- [ ] Back up `data/paper/trader.db` and the matching `data/raw/paper` tree together.
- [ ] Use a SQLite-safe backup procedure rather than copying an actively written database blindly.
- [ ] Retain multiple dated backups off the Pi.
- [ ] Perform one restore drill into a temporary location.
- [ ] Confirm restored runs can resolve their research and reasoning artifacts.
- [ ] Add disk-retention rules only after the backup process is verified.

## P1: Operational hardening after launch

These items should be completed during the first one or two weeks of paper operation.

### Service and restart tests

- [ ] Reboot the Pi and verify all required timers/services return automatically.
- [ ] Kill the trader during research and verify the run fails or recovers clearly.
- [ ] Kill it after order submission but before the response is persisted; verify reconciliation prevents a duplicate order.
- [ ] Trigger the same daily timer twice; verify duplicate-run protection works.
- [ ] Trigger the same scheduled event twice; verify it executes at most once.
- [ ] Simulate an Alpaca timeout, a Codex failure, a database lock, and a temporary network outage.
- [ ] Verify scheduled runs abandoned by a dead worker are recovered or expired according to policy.

### Ongoing order lifecycle

- [ ] Decide how frequently reconciliation should run independently of the daily job.
- [ ] Define what happens to old open or partially filled orders.
- [ ] Add an explicit stale-order cancellation policy if one is not already enforced.
- [ ] Verify market holidays, early closes, and daylight-saving transitions.
- [ ] Add end-of-day reconciliation independent of the daily job.
- [x] Add a beginner-facing HTML daily briefing (`daily_update.html`), written by the daily trader
      and filled after risk/execution with what was actually proposed, approved, or submitted.

### Operator runbook

Document the actual unit names after committing the Pi deployment files. Useful checks include:

```bash
systemctl list-timers --all
uv run trader runs list
uv run trader runs show RUN_ID
uv run trader schedule list
uv run trader status
uv run trader reconcile
```

Emergency stop:

```bash
uv run trader halt
```

The runbook still needs an explicit, safe resume procedure. Resuming should require the operator to verify the account, open orders, reconciliation, and the reason for the halt before removing the stop condition.

## P1: Missing trading capabilities

### Event-run reasoning and execution

The durable scheduler works, but a due event currently wakes an event run that completes without an event-specific Codex research/reasoning/risk/execution pipeline.

- [ ] Load the scheduled event and its evidence.
- [ ] Collect event-specific research as of the event run.
- [ ] Invoke the configured event-trader profile.
- [ ] Validate the proposal and admitted evidence.
- [ ] Run the same deterministic risk and paper execution path as the daily trader.
- [ ] Persist the complete event-to-decision-to-order audit chain.
- [ ] Apply stricter limits to extra runs per day and minimum spacing.

Until this is complete, the scheduler is useful and durable, but scheduled event follow-ups do not independently trade.

### Broader event discovery

The current automated event calendar is strongest for BEA economic releases. Add providers for:

- [ ] Company earnings dates and expected release times.
- [ ] Earnings calls and transcript availability.
- [ ] FDA decisions and biotech regulatory events.
- [ ] Investor days and scheduled company presentations.
- [ ] Other major economic releases not covered by the current source.

Every provider should preserve its raw source, retrieval time, source event ID, confidence, and reschedule/cancellation history.

### Research breadth and evidence selection

- [ ] Add bounded general web research for questions not answered by Alpaca and SEC sources.
- [ ] Keep raw evidence immutable and explicitly linked to each model invocation.
- [ ] Add source-quality scoring and contradiction handling.
- [ ] Measure whether additional research improves decisions rather than merely increasing context size.
- [ ] Consider paid or x402 sources later for narrowly defined information gaps, with per-run and daily spending caps.

The agent should receive a curated evidence set relevant to its questions, not the entire raw research corpus.

- [ ] After deployment, compare the live line with the `starter-positions` simulated book for at
      least four weeks before any human edit to `knowledge/strategy.md`.

## P2: Strategy and memory system

### Process profiles and compactor

The current steps 1–6 slice runs profiles for **simulated books only**. It adds the `single_pass`
and `research_then_adversary` catalog entries, typed cited packets, deterministic prompt composition,
role-specific context projections, and a common profile executor. The incumbent daily context,
prompt, unnamed invocation, and paper-execution gates stay unchanged. Acceptance verification is
tracked in [process_profiles_plan.md](process_profiles_plan.md), not inferred from this checklist.

Packets separate facts, attributed source claims, and interpretations. Invocation IDs, packet
hashes, local claim IDs, and every consumed predecessor make communication reconstructable;
they do not establish that the manager used or benefited from a packet. Missing collection is
unknown, not proof of source omission.

- [x] Add addressed dissent: every consumed contradiction/dissent claim is recorded as accepted,
      rejected, or deferred with admitted evidence; deferral requires a concrete trigger.
- [x] Add structured waiting records with evidence gaps, price/evidence/event triggers, future
      reconsideration conditions, and an explicit distinction between deliberate abstention, data
      unavailability, and failed evaluation.
- [x] Require relevant new inputs, a satisfied price trigger, or a due review before reopening an
      unchanged book idea. Named events remain unresolved until typed event context exists.
- [x] Add one typed, bounded model-requested follow-up collection round with cumulative budgets and
      deterministic provider/symbol/question validation. See [research design](research_pipeline_design.md).
- [ ] Add pre-outcome predictions and independent resolution procedures.
- [ ] Evaluate trade and wait decisions, inference costs, and process variants in controlled forward
      trials; hold starting conditions/configuration fixed and retain failed experiments.
      Detailed per-invocation and per-book token accounting is implemented; Codex CLI does not
      currently expose monetary pricing or billed cost.
- [ ] Convert useful packet conclusions into versioned longer-term memory with source links, without
      granting the compactor authority to mutate strategy or theses.

### Weekly strategist

Runnable: `trader weekly-run`, gated by `TRADER_STRATEGIST_ENABLED`. The review takes no broker,
proposes at most one anchored edit, and is applied only by `trader strategy approve`.

- [x] Build a weekend job that reviews the week's decisions, fills, P&L, and rejected proposals.
- [x] Let it propose edits to the central strategy document.
- [x] Store proposed changes as a reviewable diff before applying them.
- [x] Keep risk ceilings and paper-only enforcement outside the strategist's control.
- [x] Track strategy versions so later performance can be attributed to the policy in effect at the time.
- [ ] Notice repetition across reviews: nothing yet detects that the same change keeps being proposed,
      or that an approved change did not produce the effect its evaluation plan predicted.
- [ ] Schedule it. The runner is idempotent per week, but no timer invokes it.

### Future idea: weekly quantitative-tool steward

- [ ] Design a dedicated, non-executing weekly agent that reviews completed experiments and may
      propose narrowly scoped deterministic research tools—for example, factor/exposure estimates,
      return-distribution scenarios, or explicitly defined probability and calibration measures.
      Each proposal must name its purpose, causal data inputs and cutoff, output schema and units,
      parameters/version, validation and cost plan, and fail-closed behavior. A human-reviewed code
      and configuration change must incorporate any tool; it begins shadow-only, then may be tested
      in simulated books. It may never self-install code, change risk ceilings or execution policy,
      or authorize paper orders.

### Simulated books

A variant runs on its own simulated book (`trader books open`, then the daily runner evaluates
every active book against the same slate and research). Books never submit, never open theses, and
cannot contaminate the live decision line (`book_id IS NULL` on live readers).

- [x] Persist books and simulated fills; derive cash/positions by replaying fills.
- [x] Authorize book proposals with the same risk engine against the book's own account.
- [x] Isolate book failures so they cannot abort the live daily run.
- [x] Cap the active roster so variants stay cheap but not free.
- [ ] Implement the future CEO book-proposal contract and full typed inbox dispatch from
      [process_profiles_plan.md](process_profiles_plan.md#future-ceo-specification--not-implemented-by-steps-16).
      No `PROPOSE_BOOK` status or weekly prompt change is part of steps 1–6. Approval will record a
      decision only; instantiation remains human-owned, with pending/approved-unopened reservations.
- [ ] Add a bounded sequence of previous reviews, resolutions, and outstanding trials to CEO memory.
      “Continue unchanged” is a complete outcome; one action per week is a ceiling, not a quota.
- [ ] Show book equity curves in the weekly review so a variant is judged on path, not argument.
- [x] Persist flat-cash and one-time SPY buy-and-hold reference curves for every completed book
      evaluation. These are deterministic comparisons, not agent books.
- [ ] Promote or demote: move capital (or the live document) only after a book's curve earns it.
Book configuration and input attribution are part of the current slice: strategy/profile/config
versions, note and prompt hashes, model settings, evidence manifests, and simulated execution
assumptions are retained by experiment phase and evaluation. A → B → A creates three phases.
Null model selections are explicitly unpinned experiments. Outcome scoring by those phases remains
future work; the same strategy/evidence alone does not establish a causal process improvement.

### Knowledge and performance review

- [x] Add durable symbol theses, opened and closed deterministically from risk-approved proposals and
      from broker-reported positions, with every transition recorded in `knowledge_changes`.
- [x] Attribute realized outcomes to the thesis that motivated the trade, so the record can be scored
      rather than only read. Derived from fills via `thesis → proposals → orders → fills`.
- [x] Separate process quality from raw P&L in weekly evaluation. The review context supplies both,
      and the prompt requires the diagnosis to rest on process rather than outcome.
- [x] Track drawdown, rejected proposals with their reason codes, and per-thesis realized outcomes.
- [ ] Add superseded/invalidated states: `theses.status` is still binary. `ThesisOutcome.closure`
      distinguishes an exit from a vanished position via `knowledge_changes`, but a thesis proven
      wrong is still indistinguishable from one that merely ran its course.
- [ ] Link each thesis to the strategy version in effect when it opened, so outcomes can be compared
      across policy versions rather than only within one.
- [ ] Track slippage, turnover, concentration, and evidence quality.
- [ ] Add operator commands for “why do we own this?” and “what changed?”
- [ ] Build a small run/position/decision dashboard only after the underlying records are stable.

## Launch acceptance checklist

The system is ready for an initial unattended paper trial when all of these are true:

- [ ] The Pi deployment is reproducible from committed files and documented secrets setup.
- [ ] A market-hours `$25` paper canary has completed and reconciled cleanly.
- [ ] The daily timer has fired exactly once without manual intervention.
- [ ] The minute scheduler has claimed a due event exactly once.
- [ ] Failures produce an external notification.
- [ ] Emergency halt has been tested and its resume procedure is documented.
- [ ] Database and raw-artifact backup and restore have been tested.
- [ ] At least five trading days complete without duplicate orders or unexplained reconciliation issues.

During the first week, review every run and reconciliation result manually. That observation period is part of the paper-mode test, not a reason to weaken the agent's trading freedom.

## Suggested implementation order

1. Commit and verify the Pi service/timer deployment.
2. Run the market-hours submission canary.
3. Enable `TRADING_ENABLED=true` in the Pi service environment.
4. Add alerts, health checks, and backups.
5. Observe five trading days and fix operational failures.
6. Implement the event-trader pipeline.
7. Expand event sources and bounded web research.
8. Schedule the existing weekly strategist; book-only compactor integration belongs to the current
   profile slice. Do not expand weekly permissions implicitly.

## Before the first controlled book experiment

These are operator decisions, not missing code. A trial started without them is still auditable,
but its curves will not support a process comparison.

- [ ] Pin a model slug in `config/agents.yaml`. Both profiles currently set `model: null`, which
      `book_experiment_phases` records as `model_identity_pinned: false`. A provider default that
      moves mid-trial changes the variable under test.
- [ ] Bound the book stage against the market close. Books run sequentially after live execution in
      `created_at` order, and the risk engine rejects every proposal once `market_is_open` is false.
      With eight three-step books at the configured timeouts, how many decisions a book is allowed
      to make depends on its position in the queue. Start earlier, limit how many books use a
      multi-step profile, or record closed-market evaluations so they can be excluded.
- [ ] Open books on the machine that will run them. `books.strategy_document_path` and
      `operating_note_path` are resolved absolute paths, so a repository move or a database
      restored to a different location fails every book's evaluation in isolation.

## Research development order after steps 1–6

1. Complete profile/book integration checks before running any experiment.
2. Run the first controlled books against the deterministic cash and SPY curves; inspect structured
   waits and their machine-checked reopen records during the trial.
3. Add bounded follow-up research using typed conditions.
4. Record predictions and measure controlled forward book experiments, including failures and cost.
5. Extend CEO context and the human proposal inbox; only then design bounded simulated lifecycle
   automation separately from brokerage execution.
6. Design short-stock support and options support as separate reviewed instrument expansions.
   Current paper-only, long-only, no-margin rules remain binding. Swappable decision/execution
   boundaries do not authorize either expansion or real trading.
