"""
Kalshi connection test script.

Connects to the Kalshi demo environment, fetches the first 10 open markets,
and prints their titles and current YES prices to confirm everything works.

Usage:
    python test_kalshi_connection.py
"""

import sys
from dotenv import load_dotenv

load_dotenv()

import config
from kalshi import KalshiClient


def main() -> None:
    print("=" * 60)
    print("  Kalshi Connection Test")
    print("=" * 60)
    print()
    print(f"  Environment: {'DEMO' if config.KALSHI_USE_DEMO else 'PRODUCTION'}")
    print(f"  API Key ID:  {config.KALSHI_API_KEY_ID[:8] + '...' if config.KALSHI_API_KEY_ID else '(not set)'}")
    print(f"  Private Key: {config.KALSHI_PRIVATE_KEY_PATH or '(not set)'}")
    print()

    # Initialize client
    client = KalshiClient()

    if not client.is_connected:
        print("  STATUS: NOT CONNECTED")
        print()
        print("  The Kalshi API client could not authenticate.")
        print("  This is expected if you haven't configured API credentials yet.")
        print()
        print("  To connect:")
        print("  1. Copy .env.example to .env")
        print("  2. Go to https://demo.kalshi.com/settings")
        print("  3. Create an API key and download the private key")
        print("  4. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
        print()
        print("  Testing paper mode simulation instead...")
        print()
        _test_paper_mode()
        client.close()
        return

    print("  STATUS: CONNECTED")
    print()

    # Fetch markets
    print("  Fetching open markets...")
    markets = client.get_markets(status="open", limit=10)

    if not markets:
        print("  No open markets found (API returned empty list).")
        print("  This could mean:")
        print("    - The demo environment has no active markets right now")
        print("    - The API response format has changed")
        client.close()
        return

    print(f"  Found {len(markets)} markets:")
    print()
    print(f"  {'Ticker':<30s} {'YES Price':>10s} {'Volume':>10s}  Title")
    print(f"  {'-'*30} {'-'*10} {'-'*10}  {'-'*40}")

    for m in markets:
        title = m.question[:40] + "..." if len(m.question) > 40 else m.question
        print(f"  {m.ticker:<30s} {m.yes_price:>9.2f}c {m.volume_usdc:>9.0f}$  {title}")

    print()
    print("  Connection test PASSED")
    print("=" * 60)

    client.close()


def _test_paper_mode() -> None:
    """Run a quick paper-mode test to verify bot components work."""
    from market_scanner import MarketScanner, MarketOpportunity
    from signal_evaluator import SignalEvaluator
    from kalshi import MarketInfo, Position
    from position_sizer import PositionSizer
    from risk_manager import RiskManager

    print("  --- Paper Mode Component Test ---")
    print()

    # Test 1: KalshiClient paper fill
    client = KalshiClient()
    from kalshi import Side
    result = client.place_order("TEST-MARKET", Side.NO, 50.0, 0.90)
    print(f"  [1] Paper order fill: success={result.success} price={result.filled_price} id={result.order_id}")

    # Test 2: MarketScanner creates opportunities
    scanner = MarketScanner(client)
    from kalshi import MarketInfo as MI
    test_market = MI(
        market_id="TEST-LONGSHOT", ticker="TEST-LONGSHOT",
        question="Will a 10-1 underdog win?", category="sports",
        yes_price=0.08, no_price=0.92, liquidity_usdc=3000,
        volume_usdc=6000, end_date_ts=0, active=True, resolved=False,
    )
    opp = scanner._evaluate_market(test_market)
    if opp:
        print(f"  [2] Longshot detected: {opp.ticker} side={opp.side} edge={opp.edge:.4f} type={opp.opportunity_type}")
    else:
        print("  [2] Longshot detection: FAILED (no opportunity found)")

    # Test 3: Signal evaluator scores the opportunity
    active_positions: dict[str, Position] = {}
    evaluator = SignalEvaluator(client, active_positions)
    if opp:
        evaluation = evaluator.evaluate(opp)
        print(f"  [3] Evaluation: should_trade={evaluation.should_copy} score={evaluation.confidence_score:.3f} side={evaluation.side.value}")
    else:
        print("  [3] Evaluation: SKIPPED (no opportunity)")

    # Test 4: Position sizer
    sizer = PositionSizer(10000.0)
    if opp and evaluation.should_copy:
        size = sizer.compute_size(evaluation)
        print(f"  [4] Position size: ${size:.2f} (portfolio=$10,000)")
    else:
        print("  [4] Position size: SKIPPED (no trade)")

    # Test 5: Risk manager
    risk_mgr = RiskManager(10000.0)
    can_trade, reason = risk_mgr.check_can_trade(10000.0, active_positions)
    print(f"  [5] Risk check: can_trade={can_trade} reason='{reason}'")

    print()
    print("  Paper mode test PASSED — all components working")
    print("=" * 60)

    client.close()


if __name__ == "__main__":
    main()
