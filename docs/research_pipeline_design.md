# Research Pipeline Design

Status: Alpaca + SEC single-pass collection and daily-trader reasoning are implemented. Evidence
synthesis, model-requested follow-up research, and web/paid enrichment remain.

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
deterministic priority score + research planner
        |
        v
deep research for a smaller set (default 8-12, holdings always eligible)
        |
        v
normalized, deduplicated research items + immutable raw payloads
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
or retrieved later must never be inserted into an older run context.

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
4. Implemented: Alpaca market/news and official SEC ticker/submissions/company-facts providers.
5. Implemented: `trader research plan` read-only preview.
6. Implemented: collection inside `daily-run` in shadow mode, still producing `NO_ACTION`.
7. Implemented: stubbed provider, duplicate, timestamp, budget, migration, and recursive-manifest tests.

## Depth without unbounded tools

Depth should come from more rounds of *deterministically executed* collection, never from handing a
model a live network tool. Three additions get most of the available depth.

### 1. Evidence packets (research compactor)

Today `assemble_daily_context` truncates raw `normalized_text` to fit a character budget, so the
daily trader competes for context with boilerplate. A compactor pass should turn retained documents
into per-symbol packets that keep facts, attributed source claims, contradictions, and freshness
while dropping repetition. Packets are themselves hashed artifacts with their own IDs, and every
statement must carry the exact upstream `research_id`s so a packet never becomes a laundering step
for uncited claims. Truncation then removes redundancy rather than evidence.

### 2. Model-requested follow-up research

A single deterministic pass cannot know which question matters until something has been read. Add a
bounded second round:

1. Round one collects the current deterministic plan.
2. A research-planner role reads the round-one packets and returns a validated `ResearchRequest`
   list: symbol, question type, and the specific gap or contradiction being resolved.
3. Deterministic code rejects any request naming an unsupported symbol, an unadmitted provider, an
   unknown question type, or a window outside the configured freshness policy. Surviving requests
   are truncated to the remaining request, byte, item, and wall-clock budget of the *same* run.
4. Round two collects only the surviving requests. Its documents receive run-scoped evidence IDs
   exactly like round one.

The model chooses *what to ask*; configuration still decides what may be fetched and how much. Round
count is capped (two is enough to start) so the loop always terminates. Budgets must be accounted
cumulatively across rounds, which the current single-pass validators do not do.

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
