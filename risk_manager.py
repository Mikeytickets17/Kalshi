"""
Risk manager module.

Enforces drawdown limits, daily loss limits, exposure caps,
consecutive loss kill switch, and emergency halt.
"""

import logging
import time
from dataclasses import dataclass, field

import datetime

import config
from polymarket import Position, Side

logger = logging.getLogger(__name__)




@dataclass
class WalletPerformance:
    """Tracks real-time performance of a wallet source."""
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    signals_since_pause: int = 0  # Count signals seen while paused
    paused: bool = False

    @property
    def win_rate(self) -> float:
        return self.wins / max(self.trades, 1)

    def reset(self) -> None:
        """Reset stats for a new evaluation window."""
        self.trades = 0
        self.wins = 0
        self.total_pnl = 0.0
        self.consecutive_losses = 0
        self.signals_since_pause = 0
        self.paused = False


@dataclass
class RiskState:
    """Current risk state of the portfolio."""
    peak_portfolio_value: float = 0.0
    daily_start_value: float = 0.0
    daily_reset_date: str = field(default_factory=lambda: str(datetime.date.today()))
    consecutive_losses: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    halted: bool = False
    halt_reason: str = ""
    wallet_performance: dict[str, WalletPerformance] = field(default_factory=dict)


class RiskManager:
    """Manages portfolio risk with kill switches and exposure limits."""

    def __init__(self, initial_portfolio_value: float) -> None:
        self._state = RiskState(
            peak_portfolio_value=initial_portfolio_value,
            daily_start_value=initial_portfolio_value,
        )
        self._initial_value = initial_portfolio_value

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def is_halted(self) -> bool:
        return self._state.halted

    def update_portfolio_value(self, current_value: float) -> None:
        """Update risk state with current portfolio value."""
        if current_value > self._state.peak_portfolio_value:
            self._state.peak_portfolio_value = current_value

        # Reset daily tracking on calendar date change
        today = str(datetime.date.today())
        if today != self._state.daily_reset_date:
            self._state.daily_start_value = current_value
            self._state.daily_reset_date = today
            logger.info("Daily risk counters reset for %s, portfolio=%.2f", today, current_value)

    def check_can_trade(
        self,
        current_value: float,
        active_positions: dict[str, Position],
        proposed_category: str = "",
        proposed_size: float = 0.0,
        source_wallet: str = "",
    ) -> tuple[bool, str]:
        """
        Check if a new trade is allowed given current risk state.

        Returns:
            (allowed, reason) — reason is empty if allowed, otherwise explains rejection.
        """
        if self._state.halted:
            return False, f"Trading halted: {self._state.halt_reason}"

        self.update_portfolio_value(current_value)

        # Check 1: Daily loss limit
        daily_pnl_pct = (current_value - self._state.daily_start_value) / self._state.daily_start_value
        if daily_pnl_pct <= -config.DAILY_LOSS_LIMIT_PCT:
            self._halt(f"Daily loss limit hit: {daily_pnl_pct:.2%}")
            return False, self._state.halt_reason

        # Check 2: Drawdown kill switch
        drawdown = (self._state.peak_portfolio_value - current_value) / self._state.peak_portfolio_value
        if drawdown >= config.DRAWDOWN_KILL_SWITCH_PCT:
            self._halt(f"Drawdown kill switch: {drawdown:.2%} from peak")
            return False, self._state.halt_reason

        # Check 3: Consecutive losses
        if self._state.consecutive_losses >= config.CONSECUTIVE_LOSSES_KILL:
            self._halt(f"Consecutive losses kill: {self._state.consecutive_losses} in a row")
            return False, self._state.halt_reason

        # Check 4: Max concurrent positions
        if len(active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return False, f"At max concurrent positions ({config.MAX_CONCURRENT_POSITIONS})"

        # Check 5: Category exposure
        if proposed_category and proposed_size > 0:
            category_exposure = sum(
                pos.size
                for pos in active_positions.values()
                if pos.category == proposed_category
            )
            max_category = current_value * config.MAX_CATEGORY_EXPOSURE_PCT
            if category_exposure + proposed_size > max_category:
                return False, (
                    f"Category '{proposed_category}' exposure "
                    f"${category_exposure + proposed_size:.2f} would exceed "
                    f"${max_category:.2f} ({config.MAX_CATEGORY_EXPOSURE_PCT:.0%})"
                )

        # Check 6: Wallet-level performance — pause wallets with poor live results
        if source_wallet:
            if source_wallet not in self._state.wallet_performance:
                self._state.wallet_performance[source_wallet] = WalletPerformance()
            wp = self._state.wallet_performance[source_wallet]

            # If wallet is paused, count signals and reset after cooldown
            if wp.paused:
                wp.signals_since_pause += 1
                if wp.signals_since_pause >= config.WALLET_COOLDOWN_TRADES:
                    logger.info(
                        "Wallet %s cooldown expired after %d signals, resetting stats",
                        source_wallet[:10], wp.signals_since_pause,
                    )
                    wp.reset()
                else:
                    return False, (
                        f"Wallet {source_wallet[:10]} paused "
                        f"({wp.signals_since_pause}/{config.WALLET_COOLDOWN_TRADES} cooldown)"
                    )

            # Evaluate after minimum trade count
            if wp.trades >= config.WALLET_PAUSE_MIN_TRADES:
                if wp.win_rate < config.WALLET_PAUSE_WR_THRESHOLD:
                    wp.paused = True
                    wp.signals_since_pause = 0
                    return False, (
                        f"Wallet {source_wallet[:10]} live WR {wp.win_rate:.0%} "
                        f"below {config.WALLET_PAUSE_WR_THRESHOLD:.0%} after {wp.trades} trades — paused"
                    )
                if wp.consecutive_losses >= config.WALLET_PAUSE_CONSEC_LOSSES:
                    wp.paused = True
                    wp.signals_since_pause = 0
                    return False, (
                        f"Wallet {source_wallet[:10]} has {wp.consecutive_losses} "
                        f"consecutive losses — paused"
                    )

        return True, ""

    def record_trade_result(self, pnl: float, source_wallet: str = "") -> None:
        """Record the result of a closed trade."""
        self._state.total_trades += 1
        if pnl >= 0:
            self._state.winning_trades += 1
            self._state.consecutive_losses = 0
        else:
            self._state.losing_trades += 1
            self._state.consecutive_losses += 1

        # Track per-wallet performance
        if source_wallet:
            if source_wallet not in self._state.wallet_performance:
                self._state.wallet_performance[source_wallet] = WalletPerformance()
            wp = self._state.wallet_performance[source_wallet]
            wp.trades += 1
            wp.total_pnl += pnl
            if pnl >= 0:
                wp.wins += 1
                wp.consecutive_losses = 0
            else:
                wp.consecutive_losses += 1

        logger.info(
            "Trade result: pnl=%.2f consecutive_losses=%d win_rate=%.2f%%",
            pnl,
            self._state.consecutive_losses,
            (self._state.winning_trades / max(self._state.total_trades, 1)) * 100,
        )

    def check_exit_conditions(
        self,
        position: Position,
        current_value: float,
    ) -> tuple[bool, str]:
        """
        Check if a position should be exited based on risk rules.

        Returns:
            (should_exit, reason)
        """
        # Stop loss — direction-aware
        if position.avg_price > 0:
            if position.side == Side.YES:
                # YES: we profit when price rises, lose when it drops
                loss_pct = (position.avg_price - position.current_price) / position.avg_price
            else:
                # NO: we profit when price drops, lose when it rises
                loss_pct = (position.current_price - position.avg_price) / (1.0 - position.avg_price) if position.avg_price < 1.0 else 0.0
            if loss_pct >= config.STOP_LOSS_PCT:
                return True, f"Stop loss triggered: {loss_pct:.2%} loss (side={position.side.value})"

        # Emergency halt — close all positions
        if self._state.halted:
            return True, f"Emergency halt: {self._state.halt_reason}"

        return False, ""

    def _halt(self, reason: str) -> None:
        """Trigger emergency halt."""
        self._state.halted = True
        self._state.halt_reason = reason
        logger.critical("RISK HALT: %s", reason)

    def reset_halt(self) -> None:
        """Manually reset the halt state (use with caution)."""
        self._state.halted = False
        self._state.halt_reason = ""
        self._state.consecutive_losses = 0
        logger.warning("Risk halt manually reset")

    def get_summary(self) -> dict:
        """Get a summary of current risk state."""
        return {
            "halted": self._state.halted,
            "halt_reason": self._state.halt_reason,
            "peak_value": round(self._state.peak_portfolio_value, 2),
            "daily_start_value": round(self._state.daily_start_value, 2),
            "consecutive_losses": self._state.consecutive_losses,
            "total_trades": self._state.total_trades,
            "winning_trades": self._state.winning_trades,
            "losing_trades": self._state.losing_trades,
            "win_rate_pct": round(
                (self._state.winning_trades / max(self._state.total_trades, 1)) * 100, 2
            ),
        }
