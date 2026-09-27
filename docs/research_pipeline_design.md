# Research Pipeline Design

Status: Alpaca market/news collection, cleaned model-facing news excerpts, SEC filing
indexes/company facts, deterministic valuation facts with bounded monthly price history, bounded
retention of actual primary filing documents, policy-aware deep-symbol promotion, and one
model-directed follow-up round are implemented. The current
[process-profile slice](process_profiles_plan.md) adds locally verified cited synthesis for simulated
books. Web/paid enrichment remains future work. The daily-trader context schema remains unchanged;
newly retained evidence enters through the existing evidence contract, and the prompts explain how
to use the deterministic valuation baseline.

## Objective

Convert the bounded daily candidate slate into reconstructable evidence packets without allowing a
research model or external data source to access broker execution. Broad discovery and deep research
remain separate so a catalog of thousands of assets never becomes one enormous prompt.

## Proposed flow

```text
candidate_scan.json (maximum 50 symbols)
        |
        v
fast market/context pass for every candidate
        |
        v
deterministic price/liquidity/instrument screen + priority score
        |
        v
deep research for a smaller set (default 8-12, holdings always eligible)
        |
        v
normalized, deduplicated research items + immutable raw payloads
        |
        v
deterministic valuation facts for mapped deep symbols
        |
        v
bounded research-planner question -> one deterministic follow-up round
        |
        v
symbol evidence packets containing facts, interpretations, conflicts, and evidence IDs
        |
        v
portfolio decision model -> structured proposals -> deterministic risk engine
```

## Source layers

1. Broker market data: quotes, snapshots, bars, material price/volume changes, corporate actions, and
   broker news metadata.
2. Primary sources: SEC filings, company investor-relations releases, and government/regulatory
   publications.
3. Web discovery: recent reputable coverage and industry context, with the fetched page or provider
   response retained rather than relying on search snippets.
4. Optional paid/x402 providers: admitted through an explicit provider registry with per-run cost
   limits, content hashes, timestamps, and the same evidence contract as free sources.
5. Social sources: sentiment/context only, never promoted to a primary fact without corroboration.

## Persistence contract

Every collected item should have:

- `research_id` and `run_id`
- provider, source tier/type/name, URL or provider identifier
- symbols and research question
- `published_at` and `retrieved_at`
- headline and normalized text/summary
- immutable raw payload path and SHA-256 content hash
- duplicate-of reference when applicable
- fact/interpretation labels and any cited upstream evidence
- collection status, bounded error details, and provider cost

The exact research IDs supplied to a model must be recorded with that invocation. Research published
or retrieved later must never be inserted into an older run context. Alpaca news keeps the original
provider bytes in its immutable raw artifact, but its model-facing excerpt removes HTML, images,
scripts, styles, and figures, unescapes entities, and collapses whitespace. Cleaning never replaces
the retained source record.

For every mapped deep symbol, deterministic `VALUATION_FACTS` combines SEC company-facts values with
the already-admitted causal current price and at most five years of bounded adjusted monthly bars.
It records formula version, selected concepts, and every input evidence ID/content hash. TTM values
are assembled without look-ahead from derivable consecutive quarters; missing inputs become explicit
unavailability reasons. The resulting derived item follows the normal persistence, evidence-ID, raw
artifact, manifest, and citation contracts. ETFs and unmapped symbols are omitted, and the research
planner cannot request this always-on deterministic question.

## Bounded policy

The initial configuration should cap:

- candidates receiving the fast pass
- candidates receiving deep research
- questions, sources, and items per symbol
- total HTTP requests, response bytes, retries, and wall-clock time
- maximum article age by research purpose
- paid-provider spend per request and per run

Holdings, scheduled-event symbols, and major contradictions should receive priority. A model may ask
for additional research only through a validated request object; deterministic code enforces all caps
and admitted providers.

## Initial implementation slice

1. Implemented: run-scoped `research_items`, symbols/questions, and model-evidence links plus migration.
2. Implemented: strict research plan, question, document, batch, and policy models.
3. Implemented: provider-neutral bounded collection and immutable raw artifact writing.
4. Implemented: Alpaca market/news and official SEC ticker/submissions/company-facts providers,
   including bounded retrieval of actual primary documents for substantive forms and the first
   issuer-authored `EX-99` exhibit attached to retained 8-K/6-K filings.
5. Implemented: `trader research plan` read-only preview.
6. Implemented: shadow collection inside `daily-run`; collection itself has no execution interface.
   Separately gated daily reasoning can produce proposals for deterministic risk and paper execution.
7. Implemented: stubbed provider, duplicate, timestamp, budget, migration, and recursive-manifest tests.

## Depth without unbounded tools

Depth should come from more rounds of *deterministically executed* collection, never from handing a
model a live network tool. Three additions get most of the available depth.

### 1. Evidence packets (research compactor)

The book-only profile slice adds `ResearchPacket` steps in `agent/packets.py` and projections in
`agent/profile_context.py`. Raw admitted evidence is loaded once for a book evaluation, then each
step receives a projection under its own role limits. Consumed packets are reserved before trimming
raw excerpts, using the invocation boundary's canonical serialized character count. Packet roles
receive research, not portfolio accounts, strategy, or execution interfaces. `BookAgentContext`
extends the unchanged incumbent daily context with consumed packets.

Packets distinguish facts, attributed source claims, interpretations, contradictions, and unknowns.
They retain producing invocation IDs, content hashes, and local claim IDs; downstream context and
profile artifacts retain every consumed predecessor. Citations must name admitted original research
IDs. Packet/claim IDs are provenance, not newly admitted source evidence. A valid citation proves
membership in the evidence set, not that the source entails a model's interpretation.

A missing collected item is an unknown. It does not prove that a company omitted a disclosure,
and an operating note cannot turn it into that proof. Broader collection-coverage metadata and
supported source comparisons are future prerequisites for systematic omission research.

This is a bounded synthesis workflow over existing evidence, not additional collection or a
learning claim. Terminal managers now record an evidence-cited disposition for every consumed
contradiction/dissent claim and a typed waiting record for no-action. Predictions, memory updates,
and measured process comparisons remain later slices.

### 2. Model-requested follow-up research

A single deterministic pass cannot know which question matters until something has been read. The
implemented bounded second round is:

1. Round one collects the current deterministic plan.
2. A read-only `research_planner` role reads the persisted first-round evidence and returns a
   validated request list: symbol, question type, the specific gap, and its decision relevance.
3. Deterministic code rejects any request naming an unsupported symbol, an unadmitted provider, an
   unknown question type, or a window outside the configured freshness policy. Surviving requests
   are truncated to the remaining request, byte, item, and wall-clock budget of the *same* run.
4. Round two collects only the surviving requests. Current bounded choices expand company news,
   promote a newly eligible symbol into current SEC filings, or retrieve a non-overlapping older SEC
   filing window. Its documents receive run-scoped evidence IDs exactly like round one.

The model chooses *what to ask*; configuration still decides what may be fetched and how much. The
configuration fixes the follow-up maximum at one round, and request, document, byte, paid-source, and
wall-clock budgets are accounted cumulatively. The planner has no broker or execution interface.

### 3. New providers behind the same contract

`ProviderName` is a closed `Literal["alpaca", "sec"]` and `admitted_providers` must currently equal
exactly that set, so adding a source is a deliberate config and code change rather than a runtime
capability. Keep it that way. A bounded web-fetch provider should retain the fetched page rather
than a search snippet, cap page count and bytes per question, and record the resolved URL and
retrieval time. Paid/x402 providers additionally need per-request and per-run spend caps enforced
before the request, not after.

## Measuring whether depth helps

More context is not better research. Before expanding sources, record enough per run to answer
whether research changed decisions: packet count and size, how many admitted evidence IDs were
actually cited, how often follow-up requests were issued and granted, and whether cited evidence
was primary or secondary. Without that, provider expansion is unfalsifiable.


Book phase/evaluation manifests retain effective configuration and evidence provenance. These
support comparisons but do not establish causality: even equal strategy/evidence hashes can coexist
with different model outputs, portfolio histories, execution conditions, or unpinned model defaults.
Later evaluation should record inference costs, preserve failed variants, predefine comparisons,
and use forward trials. Historical date filtering cannot remove knowledge embedded in model weights.
