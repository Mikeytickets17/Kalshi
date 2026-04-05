"""
Signal evaluator module — Kalshi longshot bias strategy.

Evaluates market opportunities from the scanner based on:
  - Longshot bias: contracts under 15c are overpriced by ~40%
  - Favorite bias: contracts over 70c are underpriced by ~2-3%

Scores each opportunity 0.0 to 1.0 and passes through those above threshold.
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
    """
    Result of evaluating a market opportunity.

    Maintains the same interface as the Polymarket version so that
    position_sizer.py and notifier.py work without modification.
    """
    should_copy: bool
    confidence_score: float
    signal: MarketOpportunity       # position_sizer reads signal.wallet_weight, etc.
    market_info: Optional[MarketInfo]
    rejection_reason: str = ""
    side: Side = Side.YES
    target_price: float = 0.0


class SignalEvaluator:
    """Evaluates market opportunities using longshot bias edge calculations."""

    def __init__(
        self,
        client: KalshiClient,
        active_positions: dict[str, Position],
    ) -> None:
        self._client = client
        self._active_positions = active_positions

    def evaluate(self, opportunity: MarketOpportunity) -> EvaluationResult:
        """Evaluate a market opportunity and decide whether to trade."""
        # Fetch fresh market info if we have a live connection
        market_info: Optional[MarketInfo] = None
        if self._client.is_connected:
            market_info = self._client.get_market(opportunity.ticker)

        # In paper mode, create synthetic MarketInfo from the opportunity
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

        # Run filters
        rejection = self._apply_filters(opportunity, market_info)
        if rejection:
            logger.info(
                "Opportunity rejected: %s reason=%s", opportunity.ticker, rejection,
            )
            return EvaluationResult(
                should_copy=False,
                confidence_score=0.0,
                signal=opportunity,
                market_info=market_info,
                rejection_reason=rejection,
            )

        # Compute confidence score
        score = self._compute_confidence(opportunity, market_info)
        side = Side.NO if opportunity.side == "NO" else Side.YES
        target_price = opportunity.current_price

        should_trade = score >= config.SIGNAL_THRESHOLD
        if not should_trade:
            logger.info(
                "Opportunity below threshold: %s score=%.3f threshold=%.3f",
                opportunity.ticker, score, config.SIGNAL_THRESHOLD,
            )

        return EvaluationResult(
            should_copy=should_trade,
            confidence_score=score,
            signal=opportunity,
            market_info=market_info,
            side=side,
            target_price=target_price,
        )

    def _apply_filters(self, opp: MarketOpportunity, market: MarketInfo) -> str:
        """Apply all filters. Returns rejection reason or empty string."""
        if market.resolved:
            return "Market already resolved"
        if not market.active:
            return "Market not active"

        # Volume filter
        if market.volume_usdc < config.MIN_MARKET_VOLUME:
            return f"Volume ${market.volume_usdc:.0f} below minimum ${config.MIN_MARKET_VOLUME:.0f}"

        # Time remaining
        if market.end_date_ts > 0:
            time_remaining = market.end_date_ts - time.time()
            if time_remaining < config.MIN_TIME_REMAINING_SECONDS:
                return f"Time remaining {time_remaining:.0f}s below minimum {config.MIN_TIME_REMAINING_SECONDS}s"

        # No duplicate positions
        if opp.market_id in self._active_positions:
            return "Already have position in this market"

        # Max concurrent positions
        if len(self._active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return f"At max concurrent positions ({config.MAX_CONCURRENT_POSITIONS})"

        # Edge must be positive
        if opp.edge <= 0:
            return f"No positive edge (edge={opp.edge:.4f})"

        return ""

    def _compute_confidence(self, opp: MarketOpportunity, market: MarketInfo) -> float:
        """
        Compute a 0.0–1.0 confidence score for the opportunity.

        Scoring factors:
          - Edge magnitude:         35%  (bigger edge = higher score)
          - Volume/liquidity:       25%  (more volume = more reliable price)
          - Time remaining:         20%  (more time = more value in the bet)
          - Opportunity type bonus: 20%  (longshots with big edge score higher)
        """
        score = 0.0

        # Factor 1: Edge magnitude (0–0.35)
        # For longshots: edge can be up to ~0.05 (NO side). Normalize over 0.06.
        # For favorites: edge is typically 0.02-0.03. Normalize over 0.04.
        if opp.opportunity_type == "longshot":
            edge_normalized = min(opp.edge / 0.06, 1.0)
        else:
            edge_normalized = min(opp.edge / 0.04, 1.0)
        score += edge_normalized * 0.35

        # Factor 2: Volume/liquidity (0–0.25)
        # More volume = price is more informative and we can exit easier
        # $2k minimum, normalize up to $50k
        vol_normalized = min(market.volume_usdc / 50000.0, 1.0)
        score += vol_normalized * 0.25

        # Factor 3: Time remaining (0–0.20)
        # More time = the market hasn't been "picked over" as much
        # Normalize: 1 hour to 7 days
        if market.end_date_ts > 0:
            time_remaining = max(market.end_date_ts - time.time(), 0)
            time_normalized = min(time_remaining / (7 * 86400), 1.0)
        else:
            time_normalized = 0.5  # Unknown → assume moderate
        score += time_normalized * 0.20

        # Factor 4: Opportunity type bonus (0–0.20)
        # Longshots with cheap YES prices have bigger structural edge
        if opp.opportunity_type == "longshot":
            # Cheaper longshots (5c vs 14c) have bigger bias
            cheapness = 1.0 - (opp.current_price / (1.0 - config.LONGSHOT_MAX_PRICE))
            # current_price is NO price (high), so invert
            yes_price = 1.0 - opp.current_price
            cheapness = max(0, 1.0 - (yes_price / config.LONGSHOT_MAX_PRICE))
            type_score = min(cheapness + 0.3, 1.0)
        else:
            # Favorites above 80c have more reliable bias
            type_score = min((market.yes_price - config.FAVORITE_MIN_PRICE) / 0.20, 1.0)
        score += type_score * 0.20

        return round(min(score, 1.0), 4)
