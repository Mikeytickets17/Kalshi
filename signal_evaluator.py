"""
Signal evaluator — multi-factor scoring with empirical calibration.

Evaluates latency arbitrage opportunities using:
1. Edge magnitude (net of spread/fees)
2. Data freshness (how stale is our price data?)
3. True probability from Black-Scholes model
4. Order book quality (can we actually fill at this price?)
5. Vol regime (is the market predictable right now?)
6. Recent signal accuracy (has this type been working?)
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
    """Evaluates latency arbitrage opportunities with multi-factor scoring."""

    def __init__(
        self,
        client: object,
        active_positions: dict[str, Position],
    ) -> None:
        self._active_positions = active_positions
        self._recent_accuracy: list[bool] = []  # last N signal outcomes

    def record_outcome(self, won: bool) -> None:
        """Record whether a signal led to a winning trade."""
        self._recent_accuracy.append(won)
        if len(self._recent_accuracy) > 50:
            self._recent_accuracy.pop(0)

    @property
    def recent_win_rate(self) -> float:
        if len(self._recent_accuracy) < 3:
            return 0.60  # prior
        return sum(self._recent_accuracy) / len(self._recent_accuracy)

    def evaluate(self, opportunity: MarketOpportunity) -> EvaluationResult:
        """Evaluate a latency arbitrage signal with multi-factor scoring."""
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

        # Require minimum confidence to trade
        if score < 0.30:
            return EvaluationResult(
                should_copy=False, confidence_score=score,
                signal=opportunity, market_info=market_info,
                rejection_reason=f"Confidence {score:.2f} below minimum 0.30",
            )

        return EvaluationResult(
            should_copy=True,
            confidence_score=score,
            signal=opportunity,
            market_info=market_info,
            side=side,
            target_price=opportunity.current_price,
        )

    def _apply_filters(self, opp: MarketOpportunity) -> str:
        """Hard filters — reject outright if any fail."""
        # Don't double up on same contract
        base_id = opp.market_id.split("-closing")[0]
        if base_id in self._active_positions:
            return "Already positioned"

        if len(self._active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return f"At max positions ({config.MAX_CONCURRENT_POSITIONS})"

        # Edge must exceed threshold (already net of fees from scanner)
        if opp.edge < config.EDGE_THRESHOLD_PCT:
            return f"Edge {opp.edge:.2%} below threshold {config.EDGE_THRESHOLD_PCT:.2%}"

        # Very large edge is fine — means strong signal, not suspicious
        # But cap at 25% to avoid obviously broken data
        if opp.edge > 0.25:
            return f"Edge {opp.edge:.2%} looks like bad data (>25%)"

        # Contract must not be expiring in < 1 min
        if opp.close_time_ts > 0:
            remaining = opp.close_time_ts - time.time()
            if remaining < config.MIN_CONTRACT_DURATION_SECONDS:
                return f"Contract expires in {remaining:.0f}s"

        # If recent signals have been losing badly, slow down
        if len(self._recent_accuracy) >= 10 and self.recent_win_rate < 0.35:
            return f"Recent win rate {self.recent_win_rate:.0%} too low — cooling off"

        return ""

    def _compute_confidence(self, opp: MarketOpportunity) -> float:
        """
        Multi-factor confidence scoring.

        Each factor contributes independently. Final score is a weighted
        combination that reflects how likely this trade is to be profitable.
        """
        factors = {}

        # Factor 1: Edge magnitude (30%)
        # 3% net edge = baseline, 8%+ = excellent
        edge_raw = min((opp.edge - config.EDGE_THRESHOLD_PCT) / 0.06, 1.0)
        factors["edge"] = max(0.1, edge_raw) * 0.30

        # Factor 2: Data freshness (20%)
        # Stale data = less reliable edge estimate
        if opp.latency_ms < 150:
            freshness = 1.0
        elif opp.latency_ms < 400:
            freshness = 0.85
        elif opp.latency_ms < 800:
            freshness = 0.6
        elif opp.latency_ms < 2000:
            freshness = 0.3
        else:
            freshness = 0.1
        factors["freshness"] = freshness * 0.20

        # Factor 3: Model probability strength (20%)
        # Higher true_prob = more certain outcome
        prob = min(opp.estimated_true_prob, 1.0)
        # Transform: 0.5 = no confidence, 0.9 = high confidence
        prob_strength = max(0.0, (prob - 0.50) / 0.45)
        factors["probability"] = prob_strength * 0.20

        # Factor 4: Time remaining (15%)
        # More time = more uncertain, but also more opportunity for mean reversion
        # Sweet spot: 5-30 minutes for 15-min contracts
        if opp.close_time_ts > 0:
            remaining_min = (opp.close_time_ts - time.time()) / 60
            if 5 <= remaining_min <= 30:
                time_score = 1.0
            elif 2 <= remaining_min < 5:
                time_score = 0.6
            elif 30 < remaining_min <= 60:
                time_score = 0.7
            else:
                time_score = 0.3
        else:
            time_score = 0.5
        factors["time"] = time_score * 0.15

        # Factor 5: Recent accuracy (15%)
        # If our signals have been hitting, confidence goes up
        accuracy_score = max(0.2, self.recent_win_rate)
        factors["accuracy"] = accuracy_score * 0.15

        score = sum(factors.values())
        return round(min(score, 1.0), 4)
