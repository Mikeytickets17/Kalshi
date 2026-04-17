"""Diagnostic: dump Kalshi series/events/markets so we know what crypto exists.

Run once:  python discover_kalshi.py
Paste the output back to Claude.
"""
from dotenv import load_dotenv
load_dotenv()

import os
from pykalshi import KalshiClient

api_key_id = os.getenv("KALSHI_API_KEY_ID")
key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./mikebot.pem")
demo = os.getenv("KALSHI_USE_DEMO", "false").lower() == "true"

print(f"Connecting (demo={demo})...")
c = KalshiClient(api_key_id=api_key_id, private_key_path=key_path, demo=demo)


def g(obj, name, default=""):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def unpack(result):
    if result is None:
        return []
    if isinstance(result, dict):
        for key in ("series", "events", "markets", "data"):
            if key in result:
                return list(result[key] or [])
        return []
    if hasattr(result, "markets"):
        return list(result.markets or [])
    if hasattr(result, "events"):
        return list(result.events or [])
    if hasattr(result, "series"):
        return list(result.series or [])
    try:
        return list(result)
    except TypeError:
        return []


print("\n" + "=" * 60)
print("ALL SERIES (get_all_series)")
print("=" * 60)
try:
    series = unpack(c.get_all_series())
    print(f"Total series: {len(series)}")
    crypto = [
        s for s in series
        if any(
            kw in (str(g(s, "ticker", "")) + " " + str(g(s, "title", ""))).upper()
            for kw in ("BTC", "ETH", "BITCOIN", "ETHEREUM", "CRYPTO")
        )
    ]
    print(f"Crypto-related: {len(crypto)}")
    for s in crypto:
        print(f"  {g(s, 'ticker'):25} | {str(g(s, 'title'))[:60]}")
except Exception as e:
    print(f"FAILED: {e}")

print("\n" + "=" * 60)
print("EVENTS (get_events limit=500) — crypto only")
print("=" * 60)
try:
    events = unpack(c.get_events(limit=500))
    crypto = [
        e for e in events
        if any(kw in str(g(e, "event_ticker", "")).upper() for kw in ("BTC", "ETH"))
    ]
    print(f"Total events: {len(events)} | Crypto events: {len(crypto)}")
    for e in crypto[:20]:
        print(f"  event={g(e, 'event_ticker'):25} series={g(e, 'series_ticker'):20} title={str(g(e, 'title'))[:40]}")
except Exception as e:
    print(f"FAILED: {e}")

print("\n" + "=" * 60)
print("MARKETS (get_markets limit=1000) — series breakdown")
print("=" * 60)
try:
    markets = unpack(c.get_markets(limit=1000))
    series_set = sorted(set(str(g(m, "series_ticker", "")) for m in markets))
    print(f"Fetched {len(markets)} markets")
    print(f"Distinct series_ticker values:")
    for s in series_set:
        count = sum(1 for m in markets if g(m, "series_ticker") == s)
        print(f"  {s or '(empty)':25} count={count}")
except Exception as e:
    print(f"FAILED: {e}")

print("\nDONE.")
