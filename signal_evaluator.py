"""
Signal evaluator — 2 strategies only, tuned for profit after fees.

Longshot fade: needs edge > 3c to clear the ~1.5c fee hurdle.
Favorite lean: needs edge > 2c, high volume for reliable price.

Scores are calibrated so that only trades with genuine post-fee
edge pass the 0.50 threshold.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
from kalshi import KalshiClient, MarketInfo, Position, Side
from market_scanner import MarketOpportunity

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    """Result of evaluating a market opportunity."""
    should_copy: bool
    confidence_score: float
    signal: MarketOpportunity
    market_info: Optional[MarketInfo]
    rejection_reason: str = ""
    side: Side = Side.YES
    target_price: float = 0.0


class SignalEvaluator:
    """Evaluates longshot and favorite opportunities for post-fee profitability."""

    def __init__(
        self,
        client: KalshiClient,
        active_positions: dict[str, Position],
    ) -> None:
        self._client = client
        self._active_positions = active_positions

    def evaluate(self, opportunity: MarketOpportunity) -> EvaluationResult:
        market_info: Optional[MarketInfo] = None
        if self._client.is_connected:
            market_info = self._client.get_market(opportunity.ticker)

        if market_info is None:
            market_info = MarketInfo(
                market_id=opportunity.market_id,
                ticker=opportunity.ticker,
                question=opportunity.title,
                category=opportunity.category,
                yes_price=1.0 - opportunity.current_price if opportunity.side == "NO" else opportunity.current_price,
                no_price=opportunity.current_price if opportunity.side == "NO" else 1.0 - opportunity.current_price,
                liquidity_usdc=opportunity.volume * 0.5,
                volume_usdc=opportunity.volume,
                end_date_ts=opportunity.close_time_ts,
                active=True,
                resolved=False,
            )

        rejection = self._apply_filters(opportunity, market_info)
        if rejection:
            return EvaluationResult(
                should_copy=False, confidence_score=0.0,
                signal=opportunity, market_info=market_info,
                rejection_reason=rejection,
            )

        score = self._compute_confidence(opportunity, market_info)
        side = Side.NO if opportunity.side == "NO" else Side.YES

        should_trade = score >= config.SIGNAL_THRESHOLD

        return EvaluationResult(
            should_copy=should_trade, confidence_score=score,
            signal=opportunity, market_info=market_info,
            side=side, target_price=opportunity.current_price,
        )

    def _apply_filters(self, opp: MarketOpportunity, market: MarketInfo) -> str:
        if market.resolved:
            return "Market resolved"
        if not market.active:
            return "Market not active"
        if market.volume_usdc < config.MIN_MARKET_VOLUME:
            return f"Volume ${market.volume_usdc:.0f} < ${config.MIN_MARKET_VOLUME:.0f}"
        if market.end_date_ts > 0:
            remaining = market.end_date_ts - time.time()
            if remaining < config.MIN_TIME_REMAINING_SECONDS:
                return f"Closing in {remaining:.0f}s"
        if opp.market_id in self._active_positions:
            return "Already positioned"
        if len(self._active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return f"At max positions ({config.MAX_CONCURRENT_POSITIONS})"

        # Edge must clear the fee hurdle
        min_edge = config.LONGSHOT_MIN_EDGE if opp.opportunity_type == "longshot" else config.FAVORITE_MIN_EDGE
        if opp.edge < min_edge:
            return f"Edge {opp.edge:.4f} < min {min_edge}"

        return ""

    def _compute_confidence(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Score based on edge strength, volume, and time — tuned per strategy."""
        if opp.opportunity_type == "longshot":
            return self._score_longshot(opp, market)
        else:
            return self._score_favorite(opp, market)

    def _score_longshot(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """
        Longshot scoring — edge relative to fee hurdle is everything.

        A 3c edge just barely clears fees. A 5c edge is solid.
        Volume matters because thin markets can't be exited.
        """
        score = 0.0

        # Edge above fee hurdle (50%): net edge after 1.5c fee
        net_edge = opp.edge - 0.015
        score += min(net_edge / 0.04, 1.0) * 0.50

        # Volume (30%): $5k minimum, normalize to $50k
        score += min(market.volume_usdc / 50000, 1.0) * 0.30

        # Cheapness (20%): cheaper longshots have stronger bias
        yes_price = 1.0 - opp.current_price
        cheapness = max(0, 1.0 - (yes_price / config.LONGSHOT_MAX_PRICE))
        score += cheapness * 0.20

        return round(min(score, 1.0), 4)

    def _score_favorite(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """
        Favorite scoring — volume is critical (need reliable price discovery).

        High-volume favorites in economics/politics have the most
        reliable bias. Sports favorites are noisier.
        """
        score = 0.0

        # Edge above fee hurdle (40%)
        net_edge = opp.edge - 0.015
        score += min(net_edge / 0.03, 1.0) * 0.40

        # Volume (35%): favorites need deep markets
        score += min(market.volume_usdc / 40000, 1.0) * 0.35

        # Price strength (25%): 90c+ favorites are most reliable
        strength = min((market.yes_price - config.FAVORITE_MIN_PRICE) / 0.20, 1.0)
        score += strength * 0.25

        return round(min(score, 1.0), 4)
