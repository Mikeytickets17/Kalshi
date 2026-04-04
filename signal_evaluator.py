"""
Signal evaluator module.

Decides whether to copy a detected trade from a watched wallet
based on filters and a confidence scoring system.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
from polymarket import MarketInfo, PolymarketClient, Position, Side
from wallet_tracker import TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    """Result of evaluating a trade signal."""
    should_copy: bool
    confidence_score: float
    signal: TradeSignal
    market_info: Optional[MarketInfo]
    rejection_reason: str = ""
    side: Side = Side.YES
    target_price: float = 0.0


class SignalEvaluator:
    """Evaluates trade signals from watched wallets and decides whether to copy."""

    def __init__(
        self,
        client: PolymarketClient,
        active_positions: dict[str, Position],
    ) -> None:
        self._client = client
        self._active_positions = active_positions
        # Track recent signals per market for multi-wallet convergence
        self._recent_signals: dict[str, list[TradeSignal]] = {}
        self._signal_window_seconds: int = config.CONVERGENCE_WINDOW_SECONDS

    def evaluate(self, signal: TradeSignal) -> EvaluationResult:
        """Evaluate a trade signal and decide whether to copy it."""
        self._track_signal(signal)

        # Fetch market info
        market_info = self._client.get_market(signal.condition_id)
        if market_info is None and signal.market_id:
            market_info = self._client.get_market_by_id(signal.market_id)

        # In paper mode, create synthetic market info if API unavailable
        if market_info is None and config.PAPER_MODE:
            market_info = MarketInfo(
                market_id=signal.market_id,
                condition_id=signal.condition_id,
                question=f"Paper market {signal.market_id}",
                category="paper",
                yes_price=signal.price if signal.side == "YES" else 1.0 - signal.price,
                no_price=1.0 - signal.price if signal.side == "YES" else signal.price,
                liquidity_usdc=100000.0,
                volume_usdc=500000.0,
                end_date_ts=int(time.time()) + 86400,
                active=True,
                resolved=False,
            )

        if market_info is None:
            return EvaluationResult(
                should_copy=False,
                confidence_score=0.0,
                signal=signal,
                market_info=None,
                rejection_reason="Could not fetch market info",
            )

        # Run filters
        rejection = self._apply_filters(signal, market_info)
        if rejection:
            logger.info(
                "Signal rejected: wallet=%s market=%s reason=%s",
                signal.wallet_alias, signal.market_id, rejection,
            )
            return EvaluationResult(
                should_copy=False,
                confidence_score=0.0,
                signal=signal,
                market_info=market_info,
                rejection_reason=rejection,
            )

        # Compute confidence score
        score = self._compute_confidence(signal, market_info)
        side = Side.YES if signal.side == "YES" else Side.NO
        target_price = market_info.yes_price if side == Side.YES else market_info.no_price

        should_copy = score >= config.COPY_THRESHOLD
        if not should_copy:
            logger.info(
                "Signal below threshold: wallet=%s market=%s score=%.3f threshold=%.3f",
                signal.wallet_alias, signal.market_id, score, config.COPY_THRESHOLD,
            )

        return EvaluationResult(
            should_copy=should_copy,
            confidence_score=score,
            signal=signal,
            market_info=market_info,
            side=side,
            target_price=target_price,
        )

    def _apply_filters(self, signal: TradeSignal, market: MarketInfo) -> str:
        """Apply all filters. Returns rejection reason or empty string if passed."""
        # Filter: market must be active and not resolved
        if market.resolved:
            return "Market already resolved"
        if not market.active:
            return "Market not active"

        # Filter: minimum liquidity
        if market.liquidity_usdc < config.MIN_MARKET_LIQUIDITY_USDC:
            return (
                f"Market liquidity ${market.liquidity_usdc:.0f} "
                f"below minimum ${config.MIN_MARKET_LIQUIDITY_USDC:.0f}"
            )

        # Filter: time remaining
        time_remaining = market.end_date_ts - time.time()
        if time_remaining < config.MIN_TIME_REMAINING_SECONDS:
            return (
                f"Time remaining {time_remaining:.0f}s "
                f"below minimum {config.MIN_TIME_REMAINING_SECONDS}s"
            )

        # Filter: no duplicate position in same market
        if signal.market_id in self._active_positions:
            return "Already have position in this market"

        # Filter: odds slippage
        current_price = (
            market.yes_price if signal.side == "YES" else market.no_price
        )
        slippage = abs(current_price - signal.price)
        if slippage > config.MAX_ODDS_SLIPPAGE:
            return (
                f"Odds slippage {slippage:.4f} exceeds maximum {config.MAX_ODDS_SLIPPAGE}"
            )

        # Filter: wallet win rate
        if signal.wallet_win_rate < config.MIN_WALLET_WIN_RATE:
            return (
                f"Wallet win rate {signal.wallet_win_rate:.2f} "
                f"below minimum {config.MIN_WALLET_WIN_RATE}"
            )

        # Filter: max concurrent positions
        if len(self._active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return (
                f"At max concurrent positions ({config.MAX_CONCURRENT_POSITIONS})"
            )

        return ""

    def _compute_confidence(self, signal: TradeSignal, market: MarketInfo) -> float:
        """
        Compute a 0.0–1.0 confidence score for the signal.

        Weights:
          - Wallet win rate:       30%  (raw WR, already filtered >= MIN)
          - Position conviction:   25%  (wallet's sizing relative to portfolio)
          - Multi-wallet converge: 20%  (multiple wallets entering same market)
          - Wallet weight/trust:   25%  (our confidence weight for this wallet)
        """
        score = 0.0

        # Factor 1: Wallet win rate (0–0.30)
        # Use the raw win rate directly — filter already ensures >= MIN_WALLET_WIN_RATE
        score += min(signal.wallet_win_rate, 1.0) * 0.30

        # Factor 2: Position conviction (0–0.25)
        # wallet_portfolio_pct > 10% is maximum conviction; scale linearly
        conviction = min(signal.wallet_portfolio_pct / 0.10, 1.0)
        score += conviction * 0.25

        # Factor 3: Multi-wallet convergence (0–0.20)
        convergence_count = self._count_convergent_signals(signal.market_id, signal.side)
        convergence_score = min(convergence_count / 3.0, 1.0)
        score += convergence_score * 0.20

        # Factor 4: Wallet weight/trust score (0–0.25)
        score += signal.wallet_weight * 0.25

        return round(min(score, 1.0), 4)

    def _track_signal(self, signal: TradeSignal) -> None:
        """Track a signal for multi-wallet convergence detection."""
        now = time.time()
        if signal.market_id not in self._recent_signals:
            self._recent_signals[signal.market_id] = []

        # Prune old signals
        self._recent_signals[signal.market_id] = [
            s for s in self._recent_signals[signal.market_id]
            if now - s.timestamp < self._signal_window_seconds
        ]
        self._recent_signals[signal.market_id].append(signal)

    def _count_convergent_signals(self, market_id: str, side: str) -> int:
        """Count unique wallets that have signaled the same market AND side recently."""
        signals = self._recent_signals.get(market_id, [])
        unique_wallets = {s.wallet_address for s in signals if s.side == side}
        return len(unique_wallets)
