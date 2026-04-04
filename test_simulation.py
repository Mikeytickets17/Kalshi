"""
Simulation test for the Polymarket copy-trading bot.

Runs a full year of simulated trading through all bot components:
wallet tracker → signal evaluator → position sizer → risk manager → exits.

Uses realistic parameters: varied wallet quality, market conditions,
slippage, losing streaks, and drawdowns.
"""

import asyncio
import json
import logging
import math
import random
import sys
import time
from dataclasses import dataclass

# Ensure deterministic results for reproducibility
random.seed(42)

import config
from polymarket import MarketInfo, PolymarketClient, Position, Side
from position_sizer import PositionSizer
from risk_manager import RiskManager
from signal_evaluator import EvaluationResult, SignalEvaluator
from wallet_tracker import TradeSignal, WalletEntry

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("simulation")
logger.setLevel(logging.INFO)

# --- Simulation Parameters ---

SIM_DAYS = 365
TRADES_PER_DAY_RANGE = (2, 8)  # Random number of signals per day
INITIAL_BALANCE = 10_000.0

# Wallet profiles with realistic win rates
WALLET_PROFILES = [
    {"alias": "sharp_whale", "win_rate": 0.78, "weight": 0.85, "avg_conviction": 0.06},
    {"alias": "steady_eddie", "win_rate": 0.73, "weight": 0.75, "avg_conviction": 0.04},
    {"alias": "hot_hand", "win_rate": 0.82, "weight": 0.80, "avg_conviction": 0.07},
    {"alias": "mid_tier", "win_rate": 0.68, "weight": 0.60, "avg_conviction": 0.03},
    {"alias": "noisy_but_ok", "win_rate": 0.65, "weight": 0.55, "avg_conviction": 0.03},
]

# Market categories with different base characteristics
MARKET_CATEGORIES = [
    {"name": "politics", "base_liquidity": 200_000, "volatility": 0.15},
    {"name": "crypto", "base_liquidity": 150_000, "volatility": 0.25},
    {"name": "sports", "base_liquidity": 100_000, "volatility": 0.10},
    {"name": "economics", "base_liquidity": 80_000, "volatility": 0.12},
    {"name": "entertainment", "base_liquidity": 60_000, "volatility": 0.08},
]


@dataclass
class SimTrade:
    """Record of a simulated trade."""
    day: int
    wallet: str
    market: str
    category: str
    side: str
    entry_price: float
    exit_price: float
    size_usdc: float
    pnl: float
    confidence: float
    exit_reason: str
    won: bool


def generate_signal(
    day: int,
    wallet_profile: dict,
    market_cat: dict,
) -> tuple[TradeSignal, MarketInfo, float]:
    """Generate a realistic trade signal with known outcome probability."""
    entry_price = round(random.uniform(0.20, 0.80), 4)
    side = random.choice(["YES", "NO"])
    size = round(random.uniform(50, 800), 2)
    portfolio_pct = round(random.gauss(wallet_profile["avg_conviction"], 0.02), 4)
    portfolio_pct = max(0.01, min(0.15, portfolio_pct))

    liquidity = market_cat["base_liquidity"] * random.uniform(0.5, 2.0)
    slippage = random.uniform(0.0, 0.03)

    signal = TradeSignal(
        wallet_address=f"0x{wallet_profile['alias']}",
        wallet_alias=wallet_profile["alias"],
        wallet_weight=wallet_profile["weight"],
        market_id=f"sim-{market_cat['name']}-day{day}-{random.randint(1000,9999)}",
        condition_id=f"0xcond{day}{random.randint(100,999)}",
        side=side,
        size_usdc=size,
        price=entry_price,
        tx_hash=f"0xsim{day}",
        block_number=day * 1000,
        wallet_win_rate=wallet_profile["win_rate"],
        wallet_portfolio_pct=portfolio_pct,
    )

    market = MarketInfo(
        market_id=signal.market_id,
        condition_id=signal.condition_id,
        question=f"Simulated {market_cat['name']} market (day {day})",
        category=market_cat["name"],
        yes_price=entry_price if side == "YES" else 1.0 - entry_price,
        no_price=1.0 - entry_price if side == "YES" else entry_price,
        liquidity_usdc=liquidity,
        volume_usdc=liquidity * 3,
        end_date_ts=int(time.time()) + 86400 * random.randint(1, 30),
        active=True,
        resolved=False,
    )

    # Determine outcome: wallet's win_rate determines the probability of a win
    # Copy-trader penalty: we enter later than the wallet, so our effective edge is reduced
    copy_delay_penalty = 0.08  # ~8% WR reduction for being a follower, not leader
    effective_wr = wallet_profile["win_rate"] - copy_delay_penalty + random.gauss(0, 0.05)
    effective_wr = max(0.35, min(0.90, effective_wr))
    outcome_win = random.random() < effective_wr

    # Determine exit price based on outcome
    # Key realism factors:
    #   - Entry slippage: we pay worse than the wallet (they moved the price)
    #   - Asymmetric payoffs: wins are often smaller than full volatility (partial resolution)
    #   - Losses can exceed expected volatility (gap risk)
    entry_slippage = random.uniform(0.005, 0.025)  # 0.5-2.5% worse entry
    vol = market_cat["volatility"]
    if outcome_win:
        # Winning trade: smaller move in our favor (we enter later, exit before full resolution)
        win_dampener = random.uniform(0.4, 0.85)  # capture only 40-85% of the move
        move = abs(random.gauss(vol * win_dampener, vol * 0.3))
        move = max(move - entry_slippage, 0.005)  # slippage eats into wins
        exit_price = min(0.98, entry_price + move) if side == "YES" else max(0.02, entry_price - move)
    else:
        # Losing trade: full adverse move plus slippage compounds
        move = abs(random.gauss(vol * 0.9, vol * 0.5))
        move = move + entry_slippage  # slippage adds to losses
        exit_price = max(0.02, entry_price - move) if side == "YES" else min(0.98, entry_price + move)

    return signal, market, round(exit_price, 4)


def run_simulation() -> None:
    """Run the full simulation."""
    logger.info("=" * 70)
    logger.info("POLYMARKET COPY-TRADING BOT — 1-YEAR SIMULATION")
    logger.info("=" * 70)
    logger.info("Initial balance: $%.2f", INITIAL_BALANCE)
    logger.info("Simulation days: %d", SIM_DAYS)
    logger.info("Wallets tracked: %d", len(WALLET_PROFILES))
    logger.info("")

    # Initialize components
    client = PolymarketClient()
    active_positions: dict[str, Position] = {}
    evaluator = SignalEvaluator(client, active_positions)
    sizer = PositionSizer(INITIAL_BALANCE)
    risk_mgr = RiskManager(INITIAL_BALANCE)

    portfolio_value = INITIAL_BALANCE
    available_balance = INITIAL_BALANCE
    trade_log: list[SimTrade] = []
    signals_seen = 0
    signals_copied = 0
    signals_rejected_filter = 0
    signals_rejected_score = 0
    signals_rejected_risk = 0
    peak_value = INITIAL_BALANCE
    max_drawdown = 0.0

    # Monthly tracking
    monthly_pnl: dict[int, float] = {}
    monthly_start_values: dict[int, float] = {}

    for day in range(1, SIM_DAYS + 1):
        month = (day - 1) // 30
        if month not in monthly_start_values:
            monthly_start_values[month] = portfolio_value
            monthly_pnl[month] = 0.0

        # Reset daily risk tracking
        risk_mgr._state.daily_start_value = portfolio_value
        risk_mgr._state.daily_start_time = time.time()

        num_signals = random.randint(*TRADES_PER_DAY_RANGE)

        for _ in range(num_signals):
            wallet = random.choice(WALLET_PROFILES)
            market_cat = random.choice(MARKET_CATEGORIES)

            signal, market_info, exit_price = generate_signal(day, wallet, market_cat)
            signals_seen += 1

            # Feed synthetic market info into evaluator by temporarily patching
            # the client to return our market info
            original_get_market = client.get_market
            client.get_market = lambda cid, mi=market_info: mi  # type: ignore[assignment]

            evaluation = evaluator.evaluate(signal)
            client.get_market = original_get_market  # type: ignore[assignment]

            if not evaluation.should_copy:
                if evaluation.rejection_reason:
                    signals_rejected_filter += 1
                else:
                    signals_rejected_score += 1
                continue

            # Risk check
            can_trade, risk_reason = risk_mgr.check_can_trade(
                portfolio_value, active_positions,
                proposed_category=market_info.category,
                source_wallet=signal.wallet_address,
            )
            if not can_trade:
                signals_rejected_risk += 1
                if risk_mgr.is_halted:
                    break
                continue

            # Size the position
            sizer.portfolio_value = portfolio_value
            size_usdc = sizer.compute_size(evaluation)

            if size_usdc > available_balance:
                continue

            # Execute (paper fill)
            entry_price = signal.price
            position = Position(
                market_id=signal.market_id,
                condition_id=signal.condition_id,
                side=Side.YES if signal.side == "YES" else Side.NO,
                size=size_usdc,
                avg_price=entry_price,
                current_price=entry_price,
                source_wallet=signal.wallet_address,
                category=market_info.category,
            )
            active_positions[signal.market_id] = position
            available_balance -= size_usdc
            signals_copied += 1

            # Simulate exit: resolve the trade with the predetermined outcome
            position.current_price = exit_price
            if position.side == Side.YES:
                pnl = (exit_price - entry_price) * size_usdc
            else:
                pnl = (entry_price - exit_price) * size_usdc

            won = pnl > 0

            # Apply stop loss if loss exceeds threshold
            exit_reason = "resolution"
            if entry_price > 0:
                loss_pct = (entry_price - exit_price) / entry_price if position.side == Side.YES else (exit_price - entry_price) / entry_price
                if loss_pct >= config.STOP_LOSS_PCT:
                    # Stop loss: cap the loss
                    pnl = -size_usdc * config.STOP_LOSS_PCT
                    exit_reason = "stop_loss"
                    won = False

            # Record trade result
            available_balance += size_usdc + pnl
            portfolio_value += pnl
            monthly_pnl[month] = monthly_pnl.get(month, 0) + pnl
            risk_mgr.record_trade_result(pnl, source_wallet=signal.wallet_address)
            sizer.portfolio_value = portfolio_value

            # Track peak and drawdown
            if portfolio_value > peak_value:
                peak_value = portfolio_value
            dd = (peak_value - portfolio_value) / peak_value
            if dd > max_drawdown:
                max_drawdown = dd

            trade_log.append(SimTrade(
                day=day,
                wallet=wallet["alias"],
                market=signal.market_id,
                category=market_info.category,
                side=signal.side,
                entry_price=entry_price,
                exit_price=exit_price,
                size_usdc=size_usdc,
                pnl=round(pnl, 2),
                confidence=evaluation.confidence_score,
                exit_reason=exit_reason,
                won=won,
            ))

            del active_positions[signal.market_id]

            # Break if halted
            if risk_mgr.is_halted:
                logger.warning("Risk halt on day %d: %s", day, risk_mgr.state.halt_reason)
                risk_mgr.reset_halt()
                break

        # Weekly portfolio log
        if day % 30 == 0:
            total_trades_so_far = len(trade_log)
            wins_so_far = sum(1 for t in trade_log if t.won)
            wr = (wins_so_far / total_trades_so_far * 100) if total_trades_so_far > 0 else 0
            logger.info(
                "Day %3d | Portfolio: $%10.2f | Trades: %4d | Win Rate: %5.1f%% | PnL: $%+9.2f",
                day, portfolio_value, total_trades_so_far, wr,
                portfolio_value - INITIAL_BALANCE,
            )

    # --- Final Report ---
    total_trades = len(trade_log)
    winning_trades = sum(1 for t in trade_log if t.won)
    losing_trades = total_trades - winning_trades
    total_pnl = portfolio_value - INITIAL_BALANCE
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    avg_win = sum(t.pnl for t in trade_log if t.won) / max(winning_trades, 1)
    avg_loss = sum(t.pnl for t in trade_log if not t.won) / max(losing_trades, 1)
    profit_factor = abs(sum(t.pnl for t in trade_log if t.won)) / max(abs(sum(t.pnl for t in trade_log if not t.won)), 0.01)
    roi = (total_pnl / INITIAL_BALANCE) * 100

    # Sharpe-like ratio (daily returns)
    daily_returns: dict[int, float] = {}
    for t in trade_log:
        daily_returns[t.day] = daily_returns.get(t.day, 0) + t.pnl
    returns_list = list(daily_returns.values())
    if len(returns_list) > 1:
        avg_return = sum(returns_list) / len(returns_list)
        std_return = math.sqrt(sum((r - avg_return) ** 2 for r in returns_list) / (len(returns_list) - 1))
        sharpe = (avg_return / std_return) * math.sqrt(252) if std_return > 0 else 0
    else:
        sharpe = 0

    # Per-wallet breakdown
    wallet_stats: dict[str, dict] = {}
    for t in trade_log:
        if t.wallet not in wallet_stats:
            wallet_stats[t.wallet] = {"trades": 0, "wins": 0, "pnl": 0.0}
        wallet_stats[t.wallet]["trades"] += 1
        if t.won:
            wallet_stats[t.wallet]["wins"] += 1
        wallet_stats[t.wallet]["pnl"] += t.pnl

    # Per-category breakdown
    cat_stats: dict[str, dict] = {}
    for t in trade_log:
        if t.category not in cat_stats:
            cat_stats[t.category] = {"trades": 0, "wins": 0, "pnl": 0.0}
        cat_stats[t.category]["trades"] += 1
        if t.won:
            cat_stats[t.category]["wins"] += 1
        cat_stats[t.category]["pnl"] += t.pnl

    print("\n")
    print("=" * 70)
    print("  POLYMARKET COPY-TRADING BOT — 1-YEAR SIMULATION RESULTS")
    print("=" * 70)
    print()
    print(f"  Duration:              {SIM_DAYS} days")
    print(f"  Initial Balance:       ${INITIAL_BALANCE:>12,.2f}")
    print(f"  Final Portfolio:       ${portfolio_value:>12,.2f}")
    print(f"  Total PnL:             ${total_pnl:>+12,.2f}")
    print(f"  ROI:                   {roi:>+11.2f}%")
    print(f"  Peak Value:            ${peak_value:>12,.2f}")
    print(f"  Max Drawdown:          {max_drawdown:>11.2%}")
    print(f"  Annualized Sharpe:     {sharpe:>11.2f}")
    print()
    print("  --- Trade Statistics ---")
    print(f"  Signals Seen:          {signals_seen:>8,}")
    print(f"  Rejected (Filters):    {signals_rejected_filter:>8,}")
    print(f"  Rejected (Score):      {signals_rejected_score:>8,}")
    print(f"  Rejected (Risk):       {signals_rejected_risk:>8,}")
    print(f"  Trades Executed:       {total_trades:>8,}")
    print(f"  Winning Trades:        {winning_trades:>8,}")
    print(f"  Losing Trades:         {losing_trades:>8,}")
    print(f"  Win Rate:              {win_rate:>10.1f}%")
    print(f"  Avg Win:               ${avg_win:>+11,.2f}")
    print(f"  Avg Loss:              ${avg_loss:>+11,.2f}")
    print(f"  Profit Factor:         {profit_factor:>11.2f}")
    print()
    print("  --- Per-Wallet Breakdown ---")
    for w, s in sorted(wallet_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["wins"] / max(s["trades"], 1) * 100
        print(f"  {w:<20s}  trades={s['trades']:>4}  WR={wr:>5.1f}%  PnL=${s['pnl']:>+10,.2f}")
    print()
    print("  --- Per-Category Breakdown ---")
    for c, s in sorted(cat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["wins"] / max(s["trades"], 1) * 100
        print(f"  {c:<20s}  trades={s['trades']:>4}  WR={wr:>5.1f}%  PnL=${s['pnl']:>+10,.2f}")
    print()

    # Monthly returns
    print("  --- Monthly Returns ---")
    for m in sorted(monthly_pnl.keys()):
        start = monthly_start_values.get(m, INITIAL_BALANCE)
        ret_pct = (monthly_pnl[m] / start) * 100 if start > 0 else 0
        bar_len = int(abs(ret_pct) * 2)
        bar = ("+" * bar_len) if ret_pct >= 0 else ("-" * bar_len)
        print(f"  Month {m+1:>2}:  ${monthly_pnl[m]:>+9,.2f}  ({ret_pct:>+6.2f}%)  {bar}")
    print()
    print("=" * 70)

    # Assertions for quality
    issues: list[str] = []
    if win_rate < 55:
        issues.append(f"Win rate {win_rate:.1f}% is below 55%")
    if total_pnl < 0:
        issues.append(f"Total PnL is negative: ${total_pnl:,.2f}")
    if max_drawdown > 0.40:
        issues.append(f"Max drawdown {max_drawdown:.1%} exceeds 40%")
    if total_trades < 100:
        issues.append(f"Only {total_trades} trades executed (expected >100)")

    if issues:
        print("  ISSUES FOUND:")
        for issue in issues:
            print(f"    - {issue}")
        print()

    status = "PASS" if not issues else "NEEDS REVIEW"
    print(f"  SIMULATION STATUS: {status}")
    print("=" * 70)


if __name__ == "__main__":
    run_simulation()
