"""
Signal evaluator — multi-strategy scoring for Kalshi markets.

Evaluates 5 opportunity types with strategy-specific scoring:
  - Longshot fade, favorite lean, closing convergence,
    multi-contract arb, stale midrange

Lower threshold (0.45) to let more trades through — volume is the goal.
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
    """Multi-strategy signal evaluator."""

    def __init__(
        self,
        client: KalshiClient,
        active_positions: dict[str, Position],
    ) -> None:
        self._client = client
        self._active_positions = active_positions

    def evaluate(self, opportunity: MarketOpportunity) -> EvaluationResult:
        """Evaluate a market opportunity and decide whether to trade."""
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
        target_price = opportunity.current_price

        should_trade = score >= config.SIGNAL_THRESHOLD

        return EvaluationResult(
            should_copy=should_trade, confidence_score=score,
            signal=opportunity, market_info=market_info,
            side=side, target_price=target_price,
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

        # Strip suffixes like "-closing", "-arb", "-stale" for dedup
        base_id = opp.market_id.split("-closing")[0].split("-arb")[0].split("-stale")[0]
        if base_id in self._active_positions:
            return "Already positioned"

        if len(self._active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return f"At max positions ({config.MAX_CONCURRENT_POSITIONS})"
        if opp.edge <= 0:
            return f"No edge ({opp.edge:.4f})"
        return ""

    def _compute_confidence(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """
        Strategy-specific confidence scoring.

        Each strategy has its own scoring weights because the edge
        characteristics are different.
        """
        otype = opp.opportunity_type

        if otype == "longshot":
            return self._score_longshot(opp, market)
        elif otype == "favorite":
            return self._score_favorite(opp, market)
        elif otype == "closing":
            return self._score_closing(opp, market)
        elif otype == "multi_arb":
            return self._score_multi_arb(opp, market)
        elif otype == "stale":
            return self._score_stale(opp, market)
        else:
            return self._score_generic(opp, market)

    def _score_longshot(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Longshots: edge magnitude matters most."""
        score = 0.0
        # Edge (40%): bigger edge = better. Normalize over 0.08 (max for cheap longshots)
        score += min(opp.edge / 0.08, 1.0) * 0.40
        # Volume (25%): normalize $500 to $30k
        score += min(market.volume_usdc / 30000, 1.0) * 0.25
        # Cheapness (20%): cheaper longshots have bigger structural bias
        yes = 1.0 - opp.current_price  # YES price
        cheapness = max(0, 1.0 - (yes / config.LONGSHOT_MAX_PRICE))
        score += cheapness * 0.20
        # Time (15%): more time = haven't been picked over
        score += self._time_factor(market) * 0.15
        return round(min(score, 1.0), 4)

    def _score_favorite(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Favorites: volume and price level matter most."""
        score = 0.0
        # Edge (30%)
        score += min(opp.edge / 0.05, 1.0) * 0.30
        # Volume (30%): higher volume = more reliable price discovery
        score += min(market.volume_usdc / 30000, 1.0) * 0.30
        # Strength (20%): stronger favorites (90c+) have more reliable bias
        strength = min((market.yes_price - config.FAVORITE_MIN_PRICE) / 0.25, 1.0)
        score += strength * 0.20
        # Time (20%)
        score += self._time_factor(market) * 0.20
        return round(min(score, 1.0), 4)

    def _score_closing(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Closing drift: urgency and edge magnitude matter."""
        score = 0.0
        # Edge (45%): drift strength is the signal
        score += min(opp.edge / 0.06, 1.0) * 0.45
        # Volume (25%)
        score += min(market.volume_usdc / 20000, 1.0) * 0.25
        # Urgency (30%): closer to close = stronger signal
        if market.end_date_ts > 0:
            hours_left = max((market.end_date_ts - time.time()) / 3600, 0.1)
            urgency = min(3.0 / hours_left, 1.0)
        else:
            urgency = 0.3
        score += urgency * 0.30
        return round(min(score, 1.0), 4)

    def _score_multi_arb(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Multi-contract arb: edge is structural, high confidence."""
        score = 0.0
        # Edge (50%): arb edge is the most reliable
        score += min(opp.edge / 0.05, 1.0) * 0.50
        # Volume (30%)
        score += min(market.volume_usdc / 15000, 1.0) * 0.30
        # Base bonus (20%): arb opportunities are inherently higher quality
        score += 0.20
        return round(min(score, 1.0), 4)

    def _score_stale(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Stale midrange: lowest confidence, smallest sizing."""
        score = 0.0
        # Edge (35%)
        score += min(opp.edge / 0.03, 1.0) * 0.35
        # Staleness indicator (35%): lower volume = more stale = bigger opportunity
        staleness = max(0, 1.0 - (market.volume_usdc / 2000))
        score += staleness * 0.35
        # Time (30%)
        score += self._time_factor(market) * 0.30
        return round(min(score, 0.75), 4)  # Cap at 0.75 — never max confidence on stale

    def _score_generic(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """Fallback scoring."""
        score = min(opp.edge / 0.05, 1.0) * 0.50
        score += min(market.volume_usdc / 20000, 1.0) * 0.30
        score += self._time_factor(market) * 0.20
        return round(min(score, 1.0), 4)

    def _time_factor(self, market: MarketInfo) -> float:
        """Normalized time remaining factor (0 to 1)."""
        if market.end_date_ts <= 0:
            return 0.5
        remaining = max(market.end_date_ts - time.time(), 0)
        return min(remaining / (7 * 86400), 1.0)
