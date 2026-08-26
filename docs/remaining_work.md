# Paper Trader: Remaining Work and Raspberry Pi Handoff

Last updated: 2026-08-22

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
- Local verification of the current implementation with 223 passing tests, Ruff, and strict source mypy.

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

## P2: Strategy and memory system

### Compactor

The compactor role is configurable but is not yet part of the run lifecycle.

- [ ] Convert completed run evidence into concise, cited thesis/memory updates.
- [ ] Prevent the compactor from changing strategy or placing trades.
- [ ] Preserve superseded versions and source links.

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

### Simulated books

A variant runs on its own simulated book (`trader books open`, then the daily runner evaluates
every active book against the same slate and research). Books never submit, never open theses, and
cannot contaminate the live decision line (`book_id IS NULL` on live readers).

- [x] Persist books and simulated fills; derive cash/positions by replaying fills.
- [x] Authorize book proposals with the same risk engine against the book's own account.
- [x] Isolate book failures so they cannot abort the live daily run.
- [x] Cap the active roster so variants stay cheap but not free.
- [ ] Spawn a book from a weekly strategy proposal instead of applying the edit to the live document.
- [ ] Show book equity curves in the weekly review so a variant is judged on path, not argument.
- [ ] Promote or demote: move capital (or the live document) only after a book's curve earns it.
- [ ] Attribute book outcomes to the strategy version they opened under.

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
8. Add the compactor, and schedule the weekly strategist alongside the daily timer.

