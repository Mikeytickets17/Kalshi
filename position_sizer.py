"""
Position sizer module.

Scales copied positions to our portfolio size with adjustments
for wallet confidence, signal strength, and conviction.
"""

import logging

import config
from signal_evaluator import EvaluationResult

logger = logging.getLogger(__name__)


class PositionSizer:
    """Determines the USDC size for a copied trade."""

    def __init__(self, portfolio_value_usdc: float) -> None:
        self._portfolio_value = portfolio_value_usdc

    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value

    @portfolio_value.setter
    def portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    def compute_size(self, evaluation: EvaluationResult) -> float:
        """
        Compute the USDC size for a copied trade.

        Formula:
          base_size = portfolio * BASE_COPY_PCT
          adjusted  = base_size * wallet_weight * confidence_score * conviction_multiplier
          clamped   = clamp(adjusted, MIN_TRADE_SIZE, min(MAX_TRADE_SIZE, portfolio * MAX_SINGLE_PCT))
        """
        signal = evaluation.signal

        # Base size: percentage of portfolio
        base_size = self._portfolio_value * config.BASE_COPY_PCT

        # Adjustment 1: wallet weight (0.0–1.0)
        wallet_factor = signal.wallet_weight

        # Adjustment 2: signal confidence score (0.0–1.0)
        confidence_factor = evaluation.confidence_score

        # Adjustment 3: conviction multiplier
        # If the wallet sized >5% of their portfolio, they have high conviction
        conviction_factor = 1.0
        if signal.wallet_portfolio_pct > config.CONVICTION_THRESHOLD_PCT:
            conviction_factor = config.CONVICTION_MULTIPLIER

        adjusted_size = base_size * wallet_factor * confidence_factor * conviction_factor

        # Hard cap: max percentage of portfolio
        max_from_portfolio = self._portfolio_value * config.MAX_SINGLE_POSITION_PCT
        upper_bound = min(config.MAX_TRADE_SIZE_USDC, max_from_portfolio)

        # Clamp to bounds
        final_size = max(config.MIN_TRADE_SIZE_USDC, min(adjusted_size, upper_bound))
        final_size = round(final_size, 2)

        logger.info(
            "Position size: base=%.2f wallet_w=%.2f conf=%.3f conviction=%.1f -> %.2f USDC (portfolio=%.2f)",
            base_size, wallet_factor, confidence_factor, conviction_factor,
            final_size, self._portfolio_value,
        )
        return final_size
