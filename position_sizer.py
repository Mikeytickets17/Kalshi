"""
Position sizer — Kelly Criterion with adaptive win-rate tracking.

Instead of betting a fixed 5% on every signal, sizes each trade based on
the measured statistical edge:
  f* = (p * b - q) / b
where p = win probability, b = avg_win/avg_loss ratio, q = 1-p.

Uses half-Kelly for safety (reduces variance at cost of ~25% less return).
Tracks win rates per signal type so sizing adapts to actual performance.
"""

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)


@dataclass
class SignalStats:
    """Tracks win/loss performance for a specific signal type."""
    trades: int = 0
    wins: int = 0
    total_win_pnl: float = 0.0   # sum of all positive PnLs
    total_loss_pnl: float = 0.0  # sum of all negative PnLs (as positive)
    last_updated: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.trades < 3:
            return 0.55  # prior: assume slight edge
        return self.wins / self.trades

    @property
    def avg_win(self) -> float:
        if self.wins == 0:
            return 1.0
        return self.total_win_pnl / self.wins

    @property
    def avg_loss(self) -> float:
        losses = self.trades - self.wins
        if losses == 0:
            return 1.0
        return self.total_loss_pnl / losses

    @property
    def payoff_ratio(self) -> float:
        """Average win / average loss."""
        al = self.avg_loss
        if al <= 0:
            return 2.0  # prior
        return self.avg_win / al

    @property
    def kelly_fraction(self) -> float:
        """
        Full Kelly: f* = (p * b - q) / b
        where p = win rate, b = payoff ratio, q = 1 - p.
        """
        p = self.win_rate
        b = self.payoff_ratio
        q = 1.0 - p
        if b <= 0:
            return 0.0
        kelly = (p * b - q) / b
        return max(0.0, kelly)

    @property
    def has_edge(self) -> bool:
        """Does this signal type have a positive expected value?"""
        return self.kelly_fraction > 0.005  # at least 0.5% Kelly


class PositionSizer:
    """
    Kelly Criterion position sizer with per-signal-type tracking.

    Uses half-Kelly by default (less volatile, ~75% of optimal growth).
    Adapts sizing based on actual trading results per signal type.
    """

    def __init__(self, portfolio_value_usdc: float) -> None:
        self._portfolio_value = portfolio_value_usdc
        self._signal_stats: dict[str, SignalStats] = defaultdict(SignalStats)
        self._kelly_fraction_override: float = 0.5  # half-Kelly

    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value

    @portfolio_value.setter
    def portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    def compute_size(self, evaluation) -> float:
        """
        Compute position size using Kelly Criterion.

        For new signal types (< 3 trades), uses conservative fixed sizing.
        For established signals, uses half-Kelly based on measured edge.
        """
        signal = evaluation.signal
        signal_type = getattr(signal, "opportunity_type", "unknown")
        confidence = evaluation.confidence_score

        stats = self._signal_stats[signal_type]

        if stats.trades < 5:
            # Not enough data for Kelly — use conservative fixed sizing
            # Start small: 1-2% of portfolio, scaled by confidence
            base_pct = 0.015
            size = self._portfolio_value * base_pct * confidence
        else:
            # Kelly sizing
            kelly = stats.kelly_fraction

            if kelly <= 0:
                # No edge detected — minimum size or skip
                logger.warning(
                    "Signal type '%s' has no measured edge (kelly=%.4f, WR=%.1f%%, payoff=%.2f). Min sizing.",
                    signal_type, kelly, stats.win_rate * 100, stats.payoff_ratio,
                )
                size = config.MIN_TRADE_SIZE_USDC
            else:
                # Half-Kelly: f*/2
                fraction = kelly * self._kelly_fraction_override
                # Scale by confidence (0.5-1.0 range to avoid zeroing out)
                conf_scale = 0.5 + confidence * 0.5
                size = self._portfolio_value * fraction * conf_scale

        # Hard caps
        max_from_portfolio = self._portfolio_value * config.MAX_SINGLE_POSITION_PCT
        upper_bound = min(config.MAX_TRADE_SIZE_USDC, max_from_portfolio)
        final_size = max(config.MIN_TRADE_SIZE_USDC, min(size, upper_bound))
        final_size = round(final_size, 2)

        logger.info(
            "Size: type=%s kelly=%.4f wr=%.1f%% payoff=%.2f trades=%d -> $%.2f (portfolio=$%.2f)",
            signal_type, stats.kelly_fraction, stats.win_rate * 100,
            stats.payoff_ratio, stats.trades, final_size, self._portfolio_value,
        )
        return final_size

    def compute_scalp_size(
        self, signal_type: str, confidence: float, size_fraction: float,
    ) -> float:
        """
        Compute size for a BTC scalp trade.

        Args:
            signal_type: e.g. "cvd_divergence", "absorption", "sweep"
            confidence: 0-1 from flow analyzer
            size_fraction: 0-1 from flow analyzer (strength * confidence)
        """
        stats = self._signal_stats[signal_type]

        if stats.trades < 5:
            # Conservative: 1-3% of portfolio based on signal strength
            base_pct = 0.01 + size_fraction * 0.02
            size = self._portfolio_value * base_pct
        else:
            kelly = stats.kelly_fraction
            if kelly <= 0:
                size = config.MIN_TRADE_SIZE_USDC
            else:
                fraction = kelly * self._kelly_fraction_override * size_fraction
                size = self._portfolio_value * fraction

        max_from_portfolio = self._portfolio_value * config.MAX_SINGLE_POSITION_PCT
        upper_bound = min(config.MAX_TRADE_SIZE_USDC, max_from_portfolio)
        final_size = max(config.MIN_TRADE_SIZE_USDC, min(size, upper_bound))

        logger.info(
            "Scalp size: type=%s frac=%.3f kelly=%.4f wr=%.1f%% -> $%.2f",
            signal_type, size_fraction, stats.kelly_fraction,
            stats.win_rate * 100, final_size,
        )
        return round(final_size, 2)

    def record_result(self, signal_type: str, pnl: float, size: float) -> None:
        """Record trade result for Kelly calibration."""
        stats = self._signal_stats[signal_type]
        stats.trades += 1
        stats.last_updated = time.time()

        if pnl >= 0:
            stats.wins += 1
            stats.total_win_pnl += pnl
        else:
            stats.total_loss_pnl += abs(pnl)

        logger.info(
            "Recorded %s: pnl=$%.2f, WR=%.1f%%, payoff=%.2f, kelly=%.4f (%d trades)",
            signal_type, pnl, stats.win_rate * 100,
            stats.payoff_ratio, stats.kelly_fraction, stats.trades,
        )

    def get_stats_summary(self) -> dict[str, dict]:
        """Get summary of all signal type performance."""
        result = {}
        for sig_type, stats in self._signal_stats.items():
            result[sig_type] = {
                "trades": stats.trades,
                "win_rate": round(stats.win_rate * 100, 1),
                "payoff_ratio": round(stats.payoff_ratio, 2),
                "kelly": round(stats.kelly_fraction, 4),
                "has_edge": stats.has_edge,
                "avg_win": round(stats.avg_win, 2),
                "avg_loss": round(stats.avg_loss, 2),
            }
        return result
