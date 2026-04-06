"""
Signal evaluator — Latency arbitrage edition.

For latency arb, the "evaluation" is simpler than bias strategies:
the edge is mathematically derived from CEX vs Polymarket price
divergence. If the edge exceeds the threshold and the price feed
confidence is high, we trade.

No guessing, no bias estimates. Pure information advantage.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
from polymarket import MarketInfo, Position, Side
from market_scanner import MarketOpportunity

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    """Result of evaluating an arbitrage opportunity."""
    should_copy: bool
    confidence_score: float
    signal: MarketOpportunity
    market_info: Optional[MarketInfo]
    rejection_reason: str = ""
    side: Side = Side.YES
    target_price: float = 0.0


class SignalEvaluator:
    """Evaluates latency arbitrage opportunities."""

    def __init__(
        self,
        client: object,  # PolymarketClient (via polymarket.py shim)
        active_positions: dict[str, Position],
    ) -> None:
        self._active_positions = active_positions

    def evaluate(self, opportunity: MarketOpportunity) -> EvaluationResult:
        """Evaluate a latency arbitrage signal."""
        market_info = MarketInfo(
            market_id=opportunity.market_id,
            ticker=opportunity.ticker,
            question=opportunity.title,
            category=opportunity.category,
            yes_price=opportunity.current_price if opportunity.side == "YES" else 1.0 - opportunity.current_price,
            no_price=opportunity.current_price if opportunity.side == "NO" else 1.0 - opportunity.current_price,
            liquidity_usdc=opportunity.volume * 0.5,
            volume_usdc=opportunity.volume,
            end_date_ts=opportunity.close_time_ts,
            active=True,
            resolved=False,
        )

        rejection = self._apply_filters(opportunity)
        if rejection:
            return EvaluationResult(
                should_copy=False, confidence_score=0.0,
                signal=opportunity, market_info=market_info,
                rejection_reason=rejection,
            )

        score = self._compute_confidence(opportunity)
        side = Side.YES if opportunity.side == "YES" else Side.NO

        return EvaluationResult(
            should_copy=True,  # If it passes filters, we always trade
            confidence_score=score,
            signal=opportunity,
            market_info=market_info,
            side=side,
            target_price=opportunity.current_price,
        )

    def _apply_filters(self, opp: MarketOpportunity) -> str:
        """Filters for latency arb — fewer filters, speed matters."""
        # Don't double up on same contract
        base_id = opp.market_id.split("-closing")[0]
        if base_id in self._active_positions:
            return "Already positioned"

        if len(self._active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return f"At max positions ({config.MAX_CONCURRENT_POSITIONS})"

        # Edge must be within bounds
        if opp.edge < config.EDGE_THRESHOLD_PCT:
            return f"Edge {opp.edge:.2%} below threshold {config.EDGE_THRESHOLD_PCT:.2%}"

        if opp.edge > config.MAX_EDGE_PCT:
            return f"Edge {opp.edge:.2%} suspiciously large (>{config.MAX_EDGE_PCT:.2%})"

        # Contract must not be expiring in < 1 min
        if opp.close_time_ts > 0:
            remaining = opp.close_time_ts - time.time()
            if remaining < config.MIN_CONTRACT_DURATION_SECONDS:
                return f"Contract expires in {remaining:.0f}s"

        return ""

    def _compute_confidence(self, opp: MarketOpportunity) -> float:
        """
        Confidence for latency arb.

        Higher edge + fresher data + more time remaining = higher confidence.
        """
        score = 0.0

        # Edge magnitude (50%): 3% edge is baseline, 8%+ is excellent
        edge_norm = min((opp.edge - config.EDGE_THRESHOLD_PCT) / 0.05, 1.0)
        score += max(edge_norm, 0.1) * 0.50

        # Data freshness (30%): lower latency = higher confidence
        if opp.latency_ms < 200:
            freshness = 1.0
        elif opp.latency_ms < 500:
            freshness = 0.8
        elif opp.latency_ms < 1000:
            freshness = 0.5
        else:
            freshness = 0.2
        score += freshness * 0.30

        # Win probability (20%): from the z-score model
        score += min(opp.estimated_true_prob, 1.0) * 0.20

        return round(min(score, 1.0), 4)
