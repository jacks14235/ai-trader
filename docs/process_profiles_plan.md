# Process profiles: simulated-book implementation contract

Last updated: 2026-09-13.
Status: steps 1–6 are implemented and locally verified. The CEO section is a future specification
and does not authorize implementing that workflow now.

## Scope and boundaries

Let simulated books run bounded research teams with named processes and reconstructable evidence.
The current slice adds a catalog, packets, role-specific projections, composed book prompts, a
profile executor, and book experiment records. It does not make the incumbent trading line use
profiles. `ShadowDailyReasoningPipeline`, `DailyAgentContext`, the existing daily-trader prompt,
and the incumbent invocation identity and execution gates remain unchanged.

All new capabilities run for simulated books only. Researchers cannot submit proposals or orders;
only the terminal manager returns a `DailyDecision`, whose proposals still require deterministic
risk authorization before simulated fills. No model or profile executor receives a `Broker`.

Keep decision contracts independent of collection and execution: caller-supplied context projectors
and a terminal proposal-persistence callback compose the workflow; risk and simulation stay outside.
That separation supports a later reviewed Alpaca paper integration. It does not enable real trading,
shorts, options, margin, automatic book promotion, or any exception to current human-owned policy.

This original slice excluded addressed dissent and structured waiting; the subsequent decision-audit
slice now implements both on terminal decisions. Forecasts and outcome resolution, model-requested
collection, event-trader invocation, CEO book proposals, automatic instantiation, and scoring or
promotion based on process performance remain out of scope. See [remaining work](remaining_work.md).

## 1. Human-owned process catalog

`src/trader/agent/catalog.py` loads strict, frozen models from `config/pipelines.yaml`. Roles define
identity and permissions in `agents.yaml`; profiles define composition separately. Register the
catalog setting and include effective catalog configuration in book experiment provenance.
`trader agents validate` validates and reports both roles and profiles.

Initial catalog:

```yaml
version: 1
default_profile: single_pass
profiles:
  single_pass:
    description: One manager decision using collected research.
    steps:
      - role: daily_trader
        step: decide
        output: daily_decision
        prompt: prompts/book_trader.md
        consumes: []
  research_then_adversary:
    description: Research packet, adversarial packet, then manager decision.
    steps:
      - role: research_compactor
        step: packet
        output: research_packet
        consumes: []
      - role: research_compactor
        step: adversary
        output: research_packet
        prompt: prompts/research_adversary.md
        consumes: [packet]
      - role: daily_trader
        step: decide
        output: daily_decision
        prompt: prompts/book_trader.md
        consumes: [packet, adversary]
```

Load must fail on unknown fields, duplicate YAML keys, invalid or duplicate step names, unknown
profiles, unsupported roles/output combinations, repeated consumes, forward references, unsupported
context sources, and invalid prompt paths. Names use lowercase alphanumeric components separated
by underscores. `MAX_PROFILE_STEPS = 5` is an exact module constant. There is exactly one
`daily_decision` step, last, using `daily_trader`; packet steps use `research_compactor`.

All effective skeleton paths, including role defaults, resolve to files under project `prompts/`.
Reject absolute catalog prompt paths and symlink escapes. Recheck containment when reading prompts;
a successful earlier catalog load does not authorize a subsequently changed file. Validate every
required role is enabled before the first invocation. A catalog must not enable a role implicitly.

Keep `single_pass` as the default. Eight active three-step books plus the unchanged incumbent
single invocation cost **25 model invocations per daily run**. If a future reviewed change also
made the incumbent three-step, that would be 27. The five-step cap bounds custom profiles separately;
25 is the supplied three-step configuration's cost, not a global invocation ceiling.

## 2. Research packets and provenance

`src/trader/agent/packets.py` owns `ResearchPacket`, its cited claims, symbol sections, and the
`NamedPacket` envelope. Keep facts, attributed source claims, and interpretations separate. Include
contradictions, unknowns, dissent, and limitations as bounded fields. A fact label is a model's
classification, not a certification by Python that the source proves it.

Every cited claim has a stable local claim ID and nonempty, unique admitted evidence IDs. Its full
identity includes the producing packet/invocation, so identical local IDs in separate packets are
not interchangeable. `NamedPacket` records its catalog step, producing invocation ID, content hash,
and typed packet. Hash canonical serialized content and verify it before downstream use.

Validation fails on invented evidence IDs, unsupported symbols, duplicate claim identities, and
extra trade fields. Packet IDs and claim IDs never become new admitted primary-evidence IDs.
Managers continue to cite the original run-scoped evidence IDs. Empty dissent is permitted: the
adversary must not invent an objection merely to satisfy a quota.

Missing collected evidence means **unknown**, not proof that a company omitted a disclosure.
Operating notes cannot override that distinction. An asserted source omission requires positive
support about the source and comparison being made. Bounded collection is not exhaustive coverage.

Packet outputs live in hashed invocation artifacts and the executor's in-memory packet bag; they
write no `trade_proposals` or theses. A later packet is synthesis, never instructions granting tools
or authority. Preserve every consumed predecessor in profile artifacts, not only a singular parent.

## 3. Context projections and composed book prompts

`src/trader/agent/profile_context.py` separates immutable input loading from per-step projection:

1. Load admitted persisted research once for a book evaluation, with its scan, raw documents,
   evidence catalog, source hashes, and cutoff. Do not query research tables anew for each step.
   The caller can share an identical immutable bundle between books. Require the research plan's
   candidate symbols to exactly match the supplied scan. The scan pins collection intent; the
   causal decision cutoff advances to the latest retrieval retained in that same run, so market
   observations collected seconds later are not mistaken for hindsight. An observation or
   publication after that retrieval cutoff still fails closed.
2. Project that bundle under each step role's declared sources, `max_context_chars`, and
   `max_document_chars`. Research context exposes candidates and research, not account, positions,
   strategy, policy, or theses. Its supported-source map rejects those declarations.
3. Include the exact declared consumed packets before budgeting. Reserve whole packets and all
   fixed fields, then trim raw excerpts deterministically. Measure with the same canonical JSON
   serialization used by `invoke_role`, not an approximate or differently formatted character count.
4. Fail if the fixed context plus packets does not fit; do not drop packets silently. Validate
   run identity and source permissions again at execution.

Use `ResearchAgentContext` with `RESEARCH_CONTEXT_SOURCES` for packet steps. Use
`BookAgentContext(DailyAgentContext)` with `research_packets`, the latest active
`waiting_decisions` record, and `BOOK_CONTEXT_SOURCES` for the book manager. The waiting record
contains machine-assessed time/price/evidence changes; reopening a scoped wait requires an exact
typed reconsideration citation. The incumbent `DailyAgentContext` and daily source map stay unchanged. Book-specific
role configuration/projection supplies packet support without requiring it in the incumbent path.
The `single_pass` book manager receives an empty packet tuple.

`src/trader/agent/prompts.py` composes a human-owned skeleton and a delimited operating note.
Composition is deterministic, including a canonical absent-note representation, and the complete
prompt is hashed and retained through `invoke_role`. Notes may specialize investigation and
interpretation; they cannot relax evidence checks, tool permissions, schema, or risk policy.

Operating notes are bounded to 8,000 characters; reject unsafe control characters while allowing
normal line breaks and tabs. A supplied empty file fails; no note path is valid and means no note.
Use book-specific prompt composition for packet guidance and manager packet interpretation. Add the
research/adversary skeletons as needed; do not edit the incumbent daily-trader or weekly-strategist
prompt for this slice.

## 4. Profile execution and invocation lineage

`src/trader/agent/pipeline.py` composes the existing `invoke_role` primitive. Its callers supply a
provider, resolved skeletons, note text, role-specific projector callbacks, and `on_decision` that
persists proposals only. The executor has no collection, market-data, risk-override, or order API.

The result is definitive:

```text
ProfileRunResult
  terminal: RoleInvocationResult[DailyDecision]
  trail: tuple[str, ...]  # successful invocation IDs in catalog order
```

Retain the complete terminal result, including context, prompt, and evidence-manifest hashes.
Do not replace it with a thin decision/invocation pair. Packet-step hashes remain associated with
their own invocations. Book summaries use `terminal.invocation_id`.

For every step, resolve declared consumed packets, project context, compose the prompt, and invoke
the declared output model with validation. Only a successful terminal decision calls `on_decision`.
Namespace every book step as `book_<uuidhex>_<slug_with_underscores>_<step>`, converting slug
hyphens to underscores. The immutable book UUID prevents ambiguous slug/step concatenations
(`foo-bar` + `baz` versus `foo` + `bar_baz`). Do not use double-underscore separators: `WorkflowStep`
forbids them. Profile-local `consumes` names remain unprefixed; the incumbent invocation stays unnamed. Refuse existing artifact directories and duplicate
workflow identities rather than overwriting or retrying them.

`parent_invocation_id` is the **last declared consumed predecessor**, or `None` for a root. It is
not the complete dependency graph. Profile plan/result/failure artifacts preserve ordered steps,
invocation IDs, and **all** consumed predecessor invocation IDs and packet hashes. Together with
`workflow_trail` and invocation artifacts these reconstruct both a linear trail and merge inputs.
A failure retains the completed prefix and profile failure JSON without persisting a terminal
proposal. Recorded model failures mark the book evaluation `FAILED`; retaining an invocation failure
without resolving its evaluation is insufficient.

## 5. Simulated-book integration

`BookEvaluationPipeline` invokes the selected profile against the book's own strategy, account,
positions, prior decisions, and operating note. Collection, risk authorization, fill simulation,
and performance recording remain in their existing owners. Do not route the incumbent daily path
through `run_profile` in this slice. A book failure remains isolated from other books and the
incumbent run, while its own evaluation fails closed.

Held-position valuation accepts only symbol-matched, timezone-aware, finite, positive, uncrossed
quotes within the recorded simulator age limit. A broker-reported market-clock cutoff is carried
through context, risk, settlement, and performance. Performance and drawdown queries exclude
snapshots after the evaluation cutoff, including when a historical run is backfilled.

`books open` gains `--profile NAME` and `--operating-note PATH`; list/show expose the selection.
An unknown profile fails. Existing rows migrate to `single_pass` with no note. Book names remain
globally unique across active, paused, and retired rows, and `MAX_ACTIVE_BOOKS = 8` remains binding.
Starting cash is finite, positive, and no greater than the loaded risk configuration's
`portfolio.expected_max_equity_usd`; never duplicate the numeric ceiling in code.

Both strategy and operating-note files must resolve to readable files inside the project root,
including symlink resolution. Enforce this when opening and again when reading/synchronizing for
evaluation. An old stored path outside the root fails that book's evaluation in isolation; do not
rewrite legacy paths or grandfather escapes. This introduces explicit containment for book
strategies; it is not a claim that the old strategy loader already enforced it.

Human edits remain possible. Re-read documents each evaluation and retain the effective content
hashes; a subsequent configuration change starts a new experimental phase.

## 6. Experiment phases and evaluation manifests

Persist book configuration fields plus `BookExperimentPhase` and `BookEvaluation` records through
an Alembic migration. `src/trader/books/experiments.py` owns durable evaluation claims and phase
identity, independent of models and brokers.

A phase records the effective configuration: catalog/profile and agent configuration versions,
strategy, skeleton/composed prompts, operating note, resolved model/provider settings, risk policy,
and simulator assumptions. A null model selection must be explicitly identified as an **unpinned**
experiment: recording a provider default does not freeze the actual model. Retain canonical
manifests and their hashes, not just mutable paths.
Every profile invocation also records its book/evaluation scope, configured model profile and
reasoning effort, timestamps, and provider-reported input/cached/output/reasoning token breakdown.
Provider-reported monetary cost and pricing may be retained, but absent CLI pricing stays NULL and
must not be replaced by an unrelated API-price estimate.
An evaluation records its book/run/phase identity, cutoff, input manifest and evidence hashes,
status, terminal invocation, and failure details. Link the retained profile artifacts for ordered
lineage and all consumed predecessors. Research input changes belong to evaluations; they do not
by themselves start new configuration phases.

Reuse only the latest matching configuration phase. A sequence A → B → A has three phases, not one
resurrected A phase. Refuse duplicate book/run evaluation claims, invalid terminal invocations,
manifest mismatches, repeated resolution, and backdated insertion that would distort phase order.
Retain failures so unsuccessful configurations do not disappear from the experimental record.

Only one evaluation may remain `STARTED` for a book; enforce this with a database partial unique
index as well as service validation. Recovery is an explicit operator action:
`trader books recover-evaluation ID --reviewer NAME --note TEXT`. It requires a `FAILED` parent run
and no `STARTED` invocation belonging to that book, records the operator decision, preserves all
artifacts and settlement, and never retries the same book/run claim.

This makes process differences inspectable. The same strategy and evidence do **not** establish
causal improvement: stochastic model outputs, model versions, portfolio history, execution timing,
and simulation assumptions can differ. This slice supplies provenance for later controlled forward
experiments, not a performance claim or a claim of novelty over prior multi-agent trading work.

## Acceptance and verification

No real daily run or broker submission was used to verify this slice. Local tests use temporary
databases, migrations, stubbed model providers, simulated market data, and fake paper brokers.

- Catalog tests reject unsupported roles/outputs, cycles/forward consumes, duplicate keys/names,
  unknown defaults, path escapes, and profiles longer than five steps.
- Packet tests reject invented citations, extra trade fields, duplicate identities, hash mismatch,
  and invalid consumed lineage. Facts, source claims, and interpretations remain distinguishable.
- Projection tests show research loads once, role-specific disclosure and limits apply, consumed
  packets are reserved intact, canonical serialized size fits, scan/plan symbols match, causal
  retrieval cutoffs apply, and oversized fixed contexts fail.
- Stubbed provider tests cover one-step and three-step profiles, full terminal hashes, every merge
  predecessor, namespaced artifacts, disabled roles, and isolated packet/decision failure.
- Book tests cover default migration, CLI options, both document containment checks including legacy
  escapes, human-owned cash limits, quote validity/freshness, historical snapshot cutoffs, exact
  terminal-step ownership, duplicate evaluation, phase changes, and retained failures.
- Regression tests preserve incumbent context/prompt/invocation behavior, `book_id IS NULL` readers,
  long-only paper restrictions, and model/execution separation.
- Validate fresh and upgraded temporary databases. Run `uv run pytest`, `uv run ruff check .`, and
  `uv run mypy src`; record actual outcomes in the implementation handoff, not invented counts here.

## Future CEO specification — not implemented by steps 1–6

The existing weekly strategist continues to propose an anchored incumbent strategy edit or no
change. Do not add `PROPOSE_BOOK`, weekly context sources, or CEO prompt changes in this slice.
The following decisions resolve the previous plan's ambiguity for a future implementation.

### Inputs, policy, and reserved work

Supply catalog descriptions and all books' names/status/profile/configuration identity/cash, not
only active names. Bound the historical summaries deterministically without losing the complete
reserved-name set. Show active-book statistics through `period_end`: equity, simulated fills,
proposal counts, and configuration phases. Historical status and hashes must also come from state
known by the cutoff, not today's mutable book row.

Keep historical evaluation inputs distinct from current operational constraints: current reserved
names, available roster slots, and outstanding proposals are admission facts, not period evidence.
Pass `max_book_starting_cash_usd` from the loaded risk ceiling into weekly context and validate it;
missing or invalid policy fails closed. Do not hardcode a cash amount.

Recheck the normalized slug in the database across every book status at proposal validation and
approval. Reserve names from pending and approved-but-unopened spawn proposals as well. Permit at
most one outstanding spawn across those two states. Approval does not release the reservation.
A future implementation must provide an explicit audited link from manual instantiation to its
approved proposal, or an explicit human cancellation releasing it; until then leave it outstanding.
Do not infer fulfillment merely from an unrelated matching name, or silently expire reservations.

### Output and persistence

One weekly outcome: `NO_CHANGE`, `PROPOSE_CHANGE`, or `PROPOSE_BOOK`. The latter carries exactly one
`ProposedBook`: normalized name, catalog profile, `strategy_source=fork_live`, bounded operating
note, positive Decimal-string cash, hypothesis, evaluation plan, and revert criteria. It contains
no strategy edit. Validate all cited IDs against supplied context; require at least one cited run
when the period has decisions, and permit none for an empty period. An empty period still cannot
support an anchored strategy edit under the existing rule.

Use a distinct typed `BookSpawnProposal` reader, `get_book_spawn_proposal`; reconstruct the payload
with `ProposedBook.model_validate_json(record.after_text)` and fail on malformed data. Do not reuse
`StrategyProposal` or strategy-specific entity constants.

| KnowledgeChange column | Proposal | Approval / rejection |
| --- | --- | --- |
| `entity_type` | `book_spawn` | `book_spawn_change` |
| `entity_id` | normalized proposed slug | proposal row ID |
| `change_type` | `BOOK_SPAWN_PROPOSED` | `BOOK_SPAWN_APPROVED` / `BOOK_SPAWN_REJECTED` |
| `before_text` | empty string | empty string |
| `after_text` | `ProposedBook.model_dump_json()` | same JSON on approval; empty on rejection |
| `reason` | diagnosis, hypothesis, evaluation plan, revert criteria | reviewer and note |
| `evidence_ids_json` | cited run/proposal/thesis IDs | `[]` |

One review outcome per weekly run is the idempotency boundary across all three statuses. Resolution
is append-only and single-use. Fulfillment/cancellation needs its own explicit audited lifecycle;
it must not rewrite an approval into a rejection.

### Complete future human inbox

`trader strategy proposals|show|approve|reject` dispatches generically by proposal change type.
Strategy show displays an anchored diff; strategy approval applies the edit and requires the
resulting content hash, as today. Book show displays typed proposal JSON; book approval records
approval only, prints operating-note text and an exact `books open` command, and writes no files
or book rows. Its resolver does **not** require `applied_content_hash`. Rejection records the matching
strategy or book resolution without writing files. Unknown types fail closed.

`books open --from-proposal` and automatic file creation are outside this specification. Human
instantiation remains explicit, with the audited association described above required before an
approved proposal stops occupying the outstanding slot. CEO sequence memory, comparative scoring,
and automatic trial lifecycle changes need their own subsequent designs.
