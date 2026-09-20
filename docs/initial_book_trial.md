# Initial simulated-book trial

This trial compares decision process while holding the strategy, starting capital, evidence pack,
risk policy, simulator, and manager model constant. Cash and one-time SPY buy-and-hold remain
model-free reference curves; they are not books and consume no inference.

## Preliminary books

| Book | Process profile | Starting cash | Strategy document | Operating note |
| --- | --- | ---: | --- | --- |
| `mean-reversion` | `single_pass` | $2,000 | `knowledge/books/mean-reversion.md` | none |
| `mean-reversion-adversary` | `research_then_adversary` | $2,000 | `knowledge/books/mean-reversion.md` | none |

Despite the historical book name, the shared document is an exact copy of the incumbent broad,
evidence-led strategy. Sharing one path is deliberate: a daily run presents both books with the same
strategy bytes, and the only intended treatment difference is the catalogued process profile.

The `single_pass` manager makes one decision from admitted evidence. The adversarial profile uses a
fast research compactor, a fast adversarial pass, and the same deep manager model for the terminal
decision. Model profiles are pinned in `config/agents.yaml` so a local Codex default change cannot
start an unmarked treatment.

## What to compare

Review the books against each other and against their cash and SPY reference points. P&L alone is
not enough for a short trial. Also compare:

- structured `NO_ACTION` frequency and whether later reopenings cite a machine-observed change;
- evidence coverage, unsupported claims, and treatment of contradictory evidence;
- proposal approval/rejection rates, turnover, drawdown, and simulated fills;
- invocation failures, latency, total tokens, and tokens per completed decision; and
- whether adversarial warnings precede avoided losses or merely add cost and prose.

Do not change either preliminary book's strategy or add an operating note during the first
comparison window. A change would begin a new experiment phase and make the process comparison less
clean.

## Reserved experimental book

The distinct strategy experiment is intentionally not created here. Its implementing agent should:

1. write a separate project-contained strategy document and, only if needed, a bounded operating
   note;
2. state a falsifiable difference from the shared incumbent strategy and a dated evaluation plan;
3. use a new globally unique book name and no more than $2,500 starting cash;
4. choose a catalogued profile explicitly and record why that process fits the hypothesis; and
5. open it only after confirming the two preliminary books are active and still have matching
   strategy hashes.

The experimental book remains simulated. It cannot reach Alpaca or any other broker.
