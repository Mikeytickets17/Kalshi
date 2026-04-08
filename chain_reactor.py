"""
News Chain Reactor — Maps ONE headline to MULTIPLE trades.

Every news event has a chain reaction across markets.
This module maps each event type to its full trade cascade.

Example: "Iran ceasefire announced"
→ SHORT oil (USO) — supply restored
→ LONG airlines (JETS) — fuel costs drop
→ LONG travel (PEJ) — travel resumes
→ LONG emerging markets (EEM) — risk on
→ SHORT defense (ITA) — less military spending
→ LONG BTC — risk on sentiment
→ Kalshi YES on ceasefire contracts
→ Kalshi NO on oil above $110

That's 8 trades from ONE headline. Each one makes money independently.
"""

# Each event type maps to a list of trades with asset, side, venue, size%, and reasoning
CHAIN_REACTIONS = {
    # ═══ IRAN / MIDDLE EAST ═══
    "iran_ceasefire": [
        {"asset": "USO", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Ceasefire = oil supply restored = oil drops"},
        {"asset": "JETS", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Lower fuel = airlines profit"},
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "Peace = risk on = stocks rally"},
        {"asset": "BTC", "side": "BUY", "venue": "binance_spot", "size_pct": 0.04, "reason": "Risk on sentiment lifts crypto"},
        {"asset": "ITA", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Peace = less defense spending"},
        {"asset": "EEM", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Emerging markets rally on peace"},
    ],
    "iran_escalation": [
        {"asset": "USO", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "War = oil supply disrupted = oil spikes"},
        {"asset": "JETS", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Higher fuel = airlines suffer"},
        {"asset": "SPY", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "War = risk off = stocks sell"},
        {"asset": "LMT", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "War = defense stocks pump"},
        {"asset": "GLD", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "War = safe haven gold"},
    ],

    # ═══ FED / RATES ═══
    "rate_cut": [
        {"asset": "BTC", "side": "BUY", "venue": "binance_spot", "size_pct": 0.05, "reason": "Rate cut = cheap money = crypto pumps"},
        {"asset": "QQQ", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "Rate cut = tech stocks rally"},
        {"asset": "TLT", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Rate cut = bonds rally"},
        {"asset": "XLF", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Rate cut = banks make less on loans"},
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Rate cut = everything goes up"},
    ],
    "rate_hike": [
        {"asset": "BTC", "side": "SELL", "venue": "binance_spot", "size_pct": 0.05, "reason": "Rate hike = tight money = crypto dumps"},
        {"asset": "QQQ", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "Rate hike = growth stocks sell"},
        {"asset": "TLT", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Rate hike = bonds sell"},
        {"asset": "XLF", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Rate hike = banks profit more"},
    ],

    # ═══ INFLATION DATA ═══
    "pce_cool": [
        {"asset": "BTC", "side": "BUY", "venue": "binance_spot", "size_pct": 0.05, "reason": "Cool inflation = rate cuts coming = BTC pumps"},
        {"asset": "QQQ", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "Cool inflation = tech rallies"},
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Cool inflation = everything rallies"},
        {"asset": "TLT", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Cool inflation = bonds rally"},
    ],
    "pce_hot": [
        {"asset": "BTC", "side": "SELL", "venue": "binance_spot", "size_pct": 0.05, "reason": "Hot inflation = no rate cuts = BTC dumps"},
        {"asset": "QQQ", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "Hot inflation = tech sells"},
        {"asset": "SPY", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Hot inflation = everything sells"},
        {"asset": "TLT", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Hot inflation = bonds sell"},
    ],

    # ═══ TARIFFS / TRADE ═══
    "tariffs_new": [
        {"asset": "SPY", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "New tariffs = trade war = stocks sell"},
        {"asset": "FXI", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Tariffs hit China stocks hard"},
        {"asset": "BTC", "side": "SELL", "venue": "binance_spot", "size_pct": 0.03, "reason": "Trade war = risk off = crypto sells"},
        {"asset": "GLD", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Trade war uncertainty = gold safe haven"},
        {"asset": "DBA", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Tariffs = higher import prices = agriculture up"},
    ],
    "trade_deal": [
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.05, "reason": "Trade deal = uncertainty removed = stocks rally"},
        {"asset": "FXI", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Trade deal = China stocks pump"},
        {"asset": "BTC", "side": "BUY", "venue": "binance_spot", "size_pct": 0.03, "reason": "Trade deal = risk on = crypto up"},
        {"asset": "EEM", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Trade deal = emerging markets rally"},
    ],

    # ═══ CRYPTO SPECIFIC ═══
    "crypto_bullish": [
        {"asset": "BTC", "side": "BUY", "venue": "binance_spot", "size_pct": 0.06, "reason": "Pro-crypto news = BTC pumps"},
        {"asset": "ETH", "side": "BUY", "venue": "binance_spot", "size_pct": 0.04, "reason": "Pro-crypto lifts all boats"},
        {"asset": "COIN", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Crypto up = Coinbase profits"},
        {"asset": "MARA", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Crypto up = miners profit"},
    ],

    # ═══ OIL / ENERGY ═══
    "oil_spike": [
        {"asset": "USO", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Oil spiking"},
        {"asset": "XLE", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Oil up = energy stocks up"},
        {"asset": "JETS", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Oil up = airlines suffer"},
        {"asset": "XLY", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Oil up = consumer discretionary hurt"},
    ],
    "oil_crash": [
        {"asset": "USO", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Oil crashing"},
        {"asset": "JETS", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Oil down = airlines profit"},
        {"asset": "XLY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Oil down = consumers spend more"},
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Oil down = less inflation = stocks up"},
    ],

    # ═══ GOVERNMENT ═══
    "shutdown_starts": [
        {"asset": "SPY", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Shutdown = uncertainty = stocks dip"},
        {"asset": "GLD", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Shutdown = safe haven"},
        {"asset": "BTC", "side": "BUY", "venue": "binance_spot", "size_pct": 0.02, "reason": "Shutdown = distrust in gov = crypto up"},
    ],
    "shutdown_ends": [
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.03, "reason": "Shutdown over = uncertainty removed"},
    ],

    # ═══ EARNINGS ═══
    "earnings_beat": [
        # Stock-specific — the asset gets filled in dynamically
        {"asset": "DYNAMIC", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Earnings beat"},
        {"asset": "SPY", "side": "BUY", "venue": "alpaca_stock", "size_pct": 0.02, "reason": "Strong earnings = market confidence"},
    ],
    "earnings_miss": [
        {"asset": "DYNAMIC", "side": "SELL", "venue": "alpaca_stock", "size_pct": 0.04, "reason": "Earnings miss"},
    ],
}


def classify_event(headline: str) -> str | None:
    """Classify a headline into an event type for chain reaction trading."""
    h = headline.lower()

    # Iran
    if any(kw in h for kw in ["ceasefire", "peace deal", "peace agreement", "truce", "suspend attack", "suspend bombing"]):
        return "iran_ceasefire"
    if any(kw in h for kw in ["strike iran", "bomb iran", "attack iran", "invade", "escalat", "troops deploy"]):
        return "iran_escalation"

    # Fed / Rates
    if any(kw in h for kw in ["rate cut", "cuts rate", "fed cut", "dovish", "easing"]):
        return "rate_cut"
    if any(kw in h for kw in ["rate hike", "raise rate", "hawkish", "tightening"]):
        return "rate_hike"

    # PCE / Inflation
    if any(kw in h for kw in ["pce below", "pce cool", "pce drop", "pce fell", "inflation cool", "inflation ease", "inflation drop", "inflation below"]):
        return "pce_cool"
    if any(kw in h for kw in ["pce above", "pce hot", "pce surge", "pce rise", "inflation hot", "inflation surge", "inflation above"]):
        return "pce_hot"

    # Tariffs
    if any(kw in h for kw in ["new tariff", "tariffs imposed", "tariff announce", "trade war escalat", "50% tariff", "60% tariff"]):
        return "tariffs_new"
    if any(kw in h for kw in ["trade deal", "trade agreement", "tariffs removed", "tariffs lifted"]):
        return "trade_deal"

    # Crypto
    if any(kw in h for kw in ["bitcoin reserve", "crypto executive order", "etf approved", "etf approval", "pro-crypto"]):
        return "crypto_bullish"

    # Oil
    if any(kw in h for kw in ["oil surge", "oil spike", "oil jump", "crude rise", "opec cut"]):
        return "oil_spike"
    if any(kw in h for kw in ["oil plunge", "oil crash", "oil drop", "oil tumble", "crude fall", "crude drop"]):
        return "oil_crash"

    # Government
    if any(kw in h for kw in ["government shutdown begin", "shutdown start"]):
        return "shutdown_starts"
    if any(kw in h for kw in ["shutdown end", "shutdown avert", "government funded", "spending bill pass"]):
        return "shutdown_ends"

    # Earnings
    if any(kw in h for kw in ["earnings beat", "revenue beat", "profit beat", "earnings top", "beats estimate"]):
        return "earnings_beat"
    if any(kw in h for kw in ["earnings miss", "revenue miss", "profit miss", "disappoints", "misses estimate"]):
        return "earnings_miss"

    return None


def get_chain_trades(event_type: str) -> list[dict]:
    """Get all trades in the chain reaction for an event type."""
    return CHAIN_REACTIONS.get(event_type, [])
