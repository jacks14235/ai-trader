"""Deterministic research planning over one bounded universe scan."""

from datetime import datetime, timedelta

from trader.agent.models import SYMBOL_PATTERN
from trader.research.config import ResearchConfig
from trader.research.models import QuestionType, ResearchPlan, ResearchQuestion
from trader.universe.models import ResearchCandidate, UniverseScan


class ResearchPlanner:
    """Turn a scan into bounded questions without model inference or network access."""

    def __init__(
        self,
        config: ResearchConfig,
        *,
        sec_symbols: frozenset[str] | None = None,
    ) -> None:
        self.config = config
        self.sec_symbols = frozenset(
            self._normalize_input_symbols(
                tuple(sorted(sec_symbols or frozenset())),
                label="SEC-mapped",
            )
        )

    def fast_plan(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan:
        """Build the all-candidate market pass used before scarce deep slots are assigned."""

        provisional = self.plan(
            scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        return provisional.model_copy(
            update={
                "deep_symbols": (),
                "questions": tuple(
                    question
                    for question in provisional.questions
                    if question.question_type == "MARKET_CONTEXT"
                ),
            }
        )

    def plan_with_deep_symbols(
        self,
        scan: UniverseScan,
        *,
        deep_symbols: tuple[str, ...],
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan:
        """Finish the initial plan after the fast pass has screened deep candidates."""

        fast = self.fast_plan(
            scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        if len(deep_symbols) > self.config.selection.max_deep_symbols:
            raise ValueError("policy-aware deep selection exceeds its configured cap")
        if not set(deep_symbols).issubset(set(fast.candidate_symbols)):
            raise ValueError("policy-aware deep selection contains a non-candidate symbol")
        priorities = {question.symbol: question.priority for question in fast.questions}
        questions = list(fast.questions)
        additional_types: tuple[QuestionType, ...] = (
            "COMPANY_NEWS",
            "SEC_FILINGS",
            "VALUATION_FACTS",
        )
        additional_count = self.config.selection.max_questions_per_symbol - 1
        for symbol in deep_symbols:
            for offset, question_type in enumerate(additional_types[:additional_count], start=1):
                if (
                    question_type in {"SEC_FILINGS", "VALUATION_FACTS"}
                    and symbol not in self.sec_symbols
                ):
                    continue
                questions.append(
                    self._question(
                        symbol=symbol,
                        question_type=question_type,
                        as_of=scan.as_of,
                        priority=max(1, priorities[symbol] - offset),
                    )
                )
        return ResearchPlan(
            as_of=fast.as_of,
            candidate_symbols=fast.candidate_symbols,
            deep_symbols=deep_symbols,
            questions=tuple(questions),
        )

    def plan(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan:
        self._validate_scan_timestamps(scan)
        portfolio = self._normalize_input_symbols(portfolio_symbols, label="portfolio")
        events = self._normalize_input_symbols(event_symbols, label="event")

        eligible_symbols = [asset.symbol for asset in scan.eligible_assets]
        if len(eligible_symbols) != len(set(eligible_symbols)):
            raise ValueError("universe scan contains duplicate eligible assets")
        candidate_symbols = [candidate.symbol for candidate in scan.candidates]
        if len(candidate_symbols) != len(set(candidate_symbols)):
            raise ValueError("universe scan contains duplicate candidates")
        eligible = set(eligible_symbols)
        missing_candidates = sorted(set(candidate_symbols).difference(eligible))
        if missing_candidates:
            raise ValueError(
                "universe candidates are absent from eligible assets: "
                + ", ".join(missing_candidates)
            )

        pinned = set(portfolio).union(events)
        missing_pinned = sorted(pinned.difference(eligible))
        if missing_pinned:
            raise ValueError(
                "portfolio/event symbols are absent from the eligible universe: "
                + ", ".join(missing_pinned)
            )
        selection = self.config.selection
        if len(pinned) > selection.max_fast_candidates:
            raise ValueError("fast-candidate cap is smaller than pinned research symbols")
        if len(portfolio) > selection.max_deep_symbols:
            raise ValueError("deep-symbol cap is smaller than current portfolio holdings")

        candidate_by_symbol = {candidate.symbol: candidate for candidate in scan.candidates}
        ordered_symbols = sorted(
            set(candidate_by_symbol).union(pinned),
            key=lambda symbol: self._priority_key(
                symbol,
                candidate_by_symbol.get(symbol),
                portfolio=frozenset(portfolio),
                events=frozenset(events),
            ),
        )
        fast_symbols = tuple(ordered_symbols[: selection.max_fast_candidates])
        if not pinned.issubset(fast_symbols):
            raise RuntimeError("pinned research symbols were displaced by the candidate cap")

        deep_symbols = tuple(fast_symbols[: selection.max_deep_symbols])
        if not set(portfolio).issubset(deep_symbols):
            raise RuntimeError("portfolio holdings were displaced by the deep-research cap")

        priorities = {
            symbol: self._question_priority(
                symbol,
                rank=rank,
                candidate=candidate_by_symbol.get(symbol),
                portfolio=frozenset(portfolio),
                events=frozenset(events),
            )
            for rank, symbol in enumerate(fast_symbols, start=1)
        }
        questions: list[ResearchQuestion] = []
        for symbol in fast_symbols:
            questions.append(
                self._question(
                    symbol=symbol,
                    question_type="MARKET_CONTEXT",
                    as_of=scan.as_of,
                    priority=priorities[symbol],
                )
            )
        additional_types: tuple[QuestionType, ...] = (
            "COMPANY_NEWS",
            "SEC_FILINGS",
            "VALUATION_FACTS",
        )
        additional_count = selection.max_questions_per_symbol - 1
        for symbol in deep_symbols:
            for offset, question_type in enumerate(additional_types[:additional_count], start=1):
                if (
                    question_type in {"SEC_FILINGS", "VALUATION_FACTS"}
                    and symbol not in self.sec_symbols
                ):
                    continue
                questions.append(
                    self._question(
                        symbol=symbol,
                        question_type=question_type,
                        as_of=scan.as_of,
                        priority=max(1, priorities[symbol] - offset),
                    )
                )

        sec_request_attempts = self.config.collection.max_retries_per_request + 1
        request_cost = {
            "MARKET_CONTEXT": 2,
            "COMPANY_NEWS": 1,
            "SEC_FILINGS": (2 + (3 * self.config.collection.max_primary_filings_per_symbol))
            * sec_request_attempts,
            "SEC_FILING_HISTORY": (1 + (3 * self.config.collection.max_primary_filings_per_symbol))
            * sec_request_attempts,
            "VALUATION_FACTS": 1,
        }
        estimated_requests = sum(request_cost[question.question_type] for question in questions)
        if estimated_requests > self.config.collection.max_total_requests:
            raise RuntimeError("research plan exceeds the configured total request cap")
        per_symbol: dict[str, int] = {}
        for question in questions:
            per_symbol[question.symbol] = per_symbol.get(question.symbol, 0) + 1
        if any(count > selection.max_questions_per_symbol for count in per_symbol.values()):
            raise RuntimeError("research plan exceeds the per-symbol question cap")

        return ResearchPlan(
            as_of=scan.as_of,
            candidate_symbols=fast_symbols,
            deep_symbols=deep_symbols,
            questions=tuple(questions),
        )

    @staticmethod
    def _validate_scan_timestamps(scan: UniverseScan) -> None:
        provider_timestamps = (
            scan.most_active_volume_updated_at,
            scan.most_active_trades_updated_at,
            scan.market_movers_updated_at,
        )
        if any(timestamp > scan.as_of for timestamp in provider_timestamps):
            raise ValueError("universe scan contains screener data from the future")

    @staticmethod
    def _normalize_input_symbols(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
        normalized = tuple(value.upper().strip() for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{label} research symbols cannot contain duplicates")
        if any(not SYMBOL_PATTERN.fullmatch(value) for value in normalized):
            raise ValueError(f"invalid {label} research symbol")
        return normalized

    @staticmethod
    def _priority_key(
        symbol: str,
        candidate: ResearchCandidate | None,
        *,
        portfolio: frozenset[str],
        events: frozenset[str],
    ) -> tuple[int, int, int, int, str]:
        signal_count = 0 if candidate is None else len(candidate.signals)
        score = -1 if candidate is None else candidate.score
        return (
            0 if symbol in portfolio else 1,
            0 if symbol in events else 1,
            0 if signal_count > 1 else 1,
            -score,
            symbol,
        )

    @staticmethod
    def _question_priority(
        symbol: str,
        *,
        rank: int,
        candidate: ResearchCandidate | None,
        portfolio: frozenset[str],
        events: frozenset[str],
    ) -> int:
        if symbol in portfolio:
            return 100
        if symbol in events:
            return 95
        if candidate is not None and len(candidate.signals) > 1:
            return 85
        return max(20, 80 - rank)

    def _question(
        self,
        *,
        symbol: str,
        question_type: QuestionType,
        as_of: datetime,
        priority: int,
    ) -> ResearchQuestion:
        freshness = self.config.freshness
        if question_type == "MARKET_CONTEXT":
            window_start = as_of - timedelta(days=freshness.market_history_days)
            query = (
                f"Price and market context current within {freshness.market_context_hours} hours, "
                f"plus {freshness.market_history_days}-day liquidity and volume for {symbol}"
            )
        elif question_type == "COMPANY_NEWS":
            window_start = as_of - timedelta(days=freshness.company_news_days)
            query = f"Material company news and catalysts for {symbol}"
        elif question_type in {"SEC_FILINGS", "SEC_FILING_HISTORY"}:
            window_start = as_of - timedelta(days=freshness.sec_filings_days)
            query = f"Recent SEC filings and material disclosures for {symbol}"
        else:
            window_start = as_of - timedelta(days=366 * self.config.valuation.lookback_years)
            query = f"Deterministic valuation facts and adjusted monthly prices for {symbol}"
        return ResearchQuestion.create(
            symbol=symbol,
            question_type=question_type,
            query=query,
            window_start=window_start,
            window_end=as_of,
            priority=priority,
        )
