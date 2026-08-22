# Research Pipeline Design

Status: initial Alpaca + SEC shadow-collection slice implemented; reasoning and web enrichment remain.

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

The next slice is evidence synthesis: build compact per-symbol packets from these retained documents,
record the exact packet IDs supplied to a reasoning model, and validate a structured `NO_ACTION` or
trade-proposal response. General web discovery and optional paid/x402 providers should follow behind
the same bounded evidence contract rather than being exposed as unrestricted model tools.

Only after those artifacts can be replayed exactly should an LLM planner/synthesizer and structured
trade proposals be connected.
