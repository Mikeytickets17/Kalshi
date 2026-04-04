"""
Risk manager module.

Enforces drawdown limits, daily loss limits, exposure caps,
consecutive loss kill switch, and emergency halt.
"""

import logging
import time
from dataclasses import dataclass, field

import config
from polymarket import Position

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Current risk state of the portfolio."""
    peak_portfolio_value: float = 0.0
    daily_start_value: float = 0.0
    daily_start_time: float = field(default_factory=time.time)
    consecutive_losses: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    halted: bool = False
    halt_reason: str = ""


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

        # Reset daily tracking at midnight equivalent (every 24h)
        if time.time() - self._state.daily_start_time >= 86400:
            self._state.daily_start_value = current_value
            self._state.daily_start_time = time.time()
            logger.info("Daily risk counters reset, portfolio=%.2f", current_value)

    def check_can_trade(
        self,
        current_value: float,
        active_positions: dict[str, Position],
        proposed_category: str = "",
        proposed_size: float = 0.0,
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
                pos.size * pos.current_price
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

        return True, ""

    def record_trade_result(self, pnl: float) -> None:
        """Record the result of a closed trade."""
        self._state.total_trades += 1
        if pnl >= 0:
            self._state.winning_trades += 1
            self._state.consecutive_losses = 0
        else:
            self._state.losing_trades += 1
            self._state.consecutive_losses += 1

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
        # Stop loss
        if position.avg_price > 0:
            loss_pct = (position.avg_price - position.current_price) / position.avg_price
            if loss_pct >= config.STOP_LOSS_PCT:
                return True, f"Stop loss triggered: {loss_pct:.2%} loss"

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
