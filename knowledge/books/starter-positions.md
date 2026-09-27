# Current Strategy

## Mandate

Run an experimental, long-only paper portfolio that searches broadly and concentrates research on a
bounded candidate slate. The objective is to test whether a disciplined, evidence-led process can
identify favorable asymmetry while preserving capital and producing decisions that can be replayed
and audited.

Cash and `NO_ACTION` are active choices. The portfolio does not need to stay fully invested, maintain
a target number of positions, or trade every day. Activity is justified only by a better prospective
case than leaving the current portfolio unchanged.

This strategy guides idea selection and review. `knowledge/portfolio_policy.md` and
`config/risk.yaml` are authoritative for permitted instruments, exposure, liquidity, sizing, order,
and loss limits. Strategy reasoning may be stricter but may never override or relax those controls.

## Core decision hypothesis

The portfolio looks for a gap between the market-implied narrative and the most decision-relevant
admitted evidence. A candidate is actionable only when the record supports all of the following:

1. **Variant view:** a specific, falsifiable explanation of what appears mispriced or misunderstood.
2. **Evidence:** recent and directly relevant facts that support the view, with important adverse or
   contradictory evidence included.
3. **Recognition path:** an observable event, operating development, or information update that could
   narrow the gap within the stated time horizon.
4. **Asymmetry:** a defensible entry price and a credible account of downside if the thesis is wrong.
5. **Invalidation:** observable conditions that would break or materially weaken the thesis.
6. **Portfolio fit:** a better use of risk and capital than cash or the positions already held, after
   considering concentration and shared drivers.

A compelling company, dramatic price move, familiar ticker, or plausible story is not enough. When
the evidence cannot distinguish opportunity from uncertainty, wait.

## Evidence discipline

- Use only evidence admitted to the current run and evaluate it as of the recorded cutoff.
- Prefer primary, timely, directly relevant records. Attribute secondary-source claims and avoid
  treating repeated coverage of one disclosure as independent confirmation.
- Separate observed facts, source claims, and portfolio inference. Candidate ranks and market signals
  direct attention; they do not prove mispricing or causation.
- Compare claims with an appropriate baseline: prior period, stated expectation, historical range, or
  alternative opportunity when that baseline is present in the admitted evidence.
- Seek disconfirmation deliberately. Stale, truncated, missing, or contradictory evidence reduces
  confidence and may require `NO_ACTION`.
- Do not infer valuation from a falling price, business quality from a rising price, or event surprise
  from the event headline alone.

## Strategy families

Every new position should fit at least one family below. Family labels organize hypotheses; they are
not reasons to trade.

### `beaten_up_quality_turnaround`

Look for a business whose price or sentiment reflects persistent impairment while admitted evidence
supports a concrete improvement in trajectory.

- Favor evidence of stabilization or repair in the specific driver that caused the deterioration,
  supported by balance-sheet or operating resilience where available.
- Require a plausible reason the improvement is not yet fully recognized and a time-bounded path for
  further confirmation.
- Reject the setup when “quality” is asserted rather than evidenced, the thesis relies only on mean
  reversion, or deterioration threatens the company's ability to reach the proposed catalyst.
- Typical invalidation concerns include renewed deterioration in the key driver, failure of stated
  milestones, or evidence that the impairment is structural rather than temporary.

### `event_driven_dislocations`

Look for a time-bounded event whose observed outcome or market reaction creates a gap between price
and supported implications.

- Establish the pre-event baseline, the actual new information, and the post-event price context from
  admitted evidence.
- Require a causal link to the symbol and a reason the reaction appears incomplete or excessive.
- Reject a trade when the event has only been scheduled, the result or comparison baseline is absent,
  or acting would amount to chasing an unsupported price move.
- Typical invalidation concerns include reversal of the event's effect, evidence that the reaction
  correctly reflects second-order consequences, or passage of the expected recognition window.

### `misunderstood_second_order_beneficiaries`

Look for businesses affected indirectly by a well-evidenced change in industry, policy, input costs,
customer behavior, or capital spending.

- Map the causal chain explicitly from the change to the company's economics and identify where the
  market may be overlooking the transmission.
- Seek company-specific confirmation rather than assuming all firms with a thematic label benefit.
- Reject long causal chains, purely thematic association, or a benefit that is immaterial, too delayed,
  offset elsewhere, or already reflected in price.
- Typical invalidation concerns include failure of the transmission mechanism, offsetting cost or
  demand effects, or evidence that value accrues to another part of the chain.

### `valuation_dislocations`

Look for a mismatch between supported business prospects and the valuation or price expectations
observable in the admitted record.

- State which expectation appears too pessimistic or optimistic and the evidence needed for a rerating.
- Use relevant comparisons only when their definitions, periods, and business differences are clear.
- Reject “cheap” or “expensive” labels based solely on price decline, a single multiple, or an
  unsupported historical comparison.
- Typical invalidation concerns include weaker normalized economics, an inappropriate comparison set,
  a balance-sheet claim on the apparent upside, or evidence that the valuation reflects a durable risk.

## Portfolio decision process

Daily review starts with the existing portfolio, not the candidate list.

1. Reassess each holding against its original thesis, recognition path, and invalidation conditions.
2. Identify the strongest candidate cases and the strongest reasons each may be wrong.
3. Compare proposed additions with cash and current holdings on evidence quality, prospective
   asymmetry, time horizon, and overlapping drivers.
4. Prefer fewer well-supported decisions to marginal diversification or quota filling.
5. Express uncertainty through waiting, watchlisting, or smaller proposed exposure rather than by
   making a weak thesis sound precise.

### Starter positions

A starter proposal may use `target_position_pct` of 5 or less when the variant view, evidence,
recognition path, invalidation, and portfolio fit are all satisfied, but asymmetry is only partially
established. It still requires a cited limit price and explicit invalidation conditions, and may be
added to only after new confirming evidence. `NO_ACTION` remains the default when these conditions
are not met.

Trade proposals use limit prices supported by admitted price and thesis evidence. The requested size
should reflect thesis quality, downside uncertainty, existing exposure, correlation, and the ability
to add only after new confirmation. Deterministic risk policy remains the final authority and may
reject or normalize any proposal.

## Position management and exits

A holding is a continuing allocation decision, not a commitment to defend the original thesis.

- **Add** only when new admitted evidence improves the prospective case or when price improves the
  asymmetry without a corresponding thesis deterioration. A lower price by itself is not confirmation.
- **Reduce or exit** when an invalidation condition occurs, the recognition path materially weakens,
  the supported downside grows, the acceptable time window passes, or another use of capital offers a
  clearly stronger prospective case.
- Do not sell solely because of an unrealized loss or hold solely to avoid realizing one. Do not let a
  favorable past outcome substitute for a current thesis.
- When evidence is ambiguous but not invalidating, prefer explicit monitoring criteria over narrative
  drift.

## Review and change control

Evaluate the strategy on process as well as outcomes: evidence quality, fidelity to the recorded
cutoff, clarity of the variant view, treatment of counterarguments, calibration, invalidation quality,
turnover, and consistency of `NO_ACTION` decisions. Separate repeatable decisions from luck and avoid
drawing conclusions from a small or correlated sample.

This document is human-owned. The disabled weekly strategist may propose a versioned change for human
review, supported by exact audited records and an evaluation or reversion plan. No agent may silently
rewrite this document, change portfolio policy, or weaken deterministic controls.
