"""
Advanced order flow analysis engine for BTC scalping.

This is the PRIMARY signal generator. It combines:
1. Cumulative Volume Delta (CVD) — net aggressive buying vs selling
2. VWAP + deviation bands — mean reversion reference
3. Absorption detection — passive orders soaking up aggression
4. Sweep detection — aggressive orders eating through the book
5. Realized volatility — adaptive thresholds per regime
6. Delta divergence — CVD disagrees with price direction

The bot does NOT trade on news or sentiment alone. Order flow is the truth.
Price is a lagging indicator. Flow is the leading indicator.
"""

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FlowTick:
    """A single aggressive trade from the exchange."""
    price: float
    qty: float
    usd: float
    side: str          # "buy" (taker bought) or "sell" (taker sold)
    ts: float          # unix timestamp with ms precision
    is_large: bool = False  # flagged if usd > dynamic threshold


@dataclass
class FlowSignal:
    """Output of the flow analyzer — the scalp signal."""
    direction: str         # "long", "short", "neutral"
    strength: float        # 0.0–1.0, how strong the signal is
    confidence: float      # 0.0–1.0, how reliable given current conditions
    signal_type: str       # primary reason for the signal
    details: str           # human-readable explanation

    # Components that contributed
    cvd_slope: float = 0.0         # positive = buying, negative = selling
    cvd_divergence: float = 0.0    # divergence from price direction
    vwap_distance_pct: float = 0.0 # distance from VWAP as % of price
    absorption_score: float = 0.0  # -1 (ask absorbed) to +1 (bid absorbed)
    sweep_score: float = 0.0       # -1 (sell sweep) to +1 (buy sweep)
    book_pressure: float = 0.0     # -1 (ask heavy) to +1 (bid heavy)
    realized_vol: float = 0.0      # annualized realized vol
    vol_regime: str = "normal"     # "low", "normal", "high", "extreme"
    price_momentum: float = 0.0    # short-term price change %

    @property
    def is_actionable(self) -> bool:
        return self.direction != "neutral" and self.strength >= 0.55 and self.confidence >= 0.45


@dataclass
class ScalpDecision:
    """Final trade decision for the BTC scalp strategy."""
    action: str            # "enter_long", "enter_short", "exit", "hold"
    asset: str
    confidence: float
    size_fraction: float   # 0.0–1.0, fraction of max position size
    reason: str
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    signal: Optional[FlowSignal] = None


# ---------------------------------------------------------------------------
# Cumulative Volume Delta (CVD) tracker
# ---------------------------------------------------------------------------

class CVDTracker:
    """
    Tracks Cumulative Volume Delta — the running sum of
    (aggressive buy volume - aggressive sell volume).

    CVD rising while price is flat = stealth accumulation → bullish
    CVD falling while price is flat = stealth distribution → bearish
    CVD diverging from price trend = reversal incoming
    """

    def __init__(self, window_seconds: float = 300.0) -> None:
        self._ticks: deque[FlowTick] = deque(maxlen=50000)
        self._window = window_seconds
        self._cvd = 0.0  # raw cumulative delta
        self._cvd_history: deque[tuple[float, float]] = deque(maxlen=3000)  # (ts, cvd)

    def add_tick(self, tick: FlowTick) -> None:
        self._ticks.append(tick)
        delta = tick.usd if tick.side == "buy" else -tick.usd
        self._cvd += delta
        self._cvd_history.append((tick.ts, self._cvd))

    @property
    def current(self) -> float:
        return self._cvd

    def slope(self, seconds: float = 10.0) -> float:
        """CVD slope over last N seconds. Positive = net buying accelerating."""
        now = time.time()
        recent = [(ts, v) for ts, v in self._cvd_history if now - ts <= seconds]
        if len(recent) < 2:
            return 0.0
        dt = recent[-1][0] - recent[0][0]
        if dt <= 0:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / dt

    def window_delta(self, seconds: float = 30.0) -> float:
        """Net delta over a time window (in USD)."""
        now = time.time()
        recent = [t for t in self._ticks if now - t.ts <= seconds]
        buy_vol = sum(t.usd for t in recent if t.side == "buy")
        sell_vol = sum(t.usd for t in recent if t.side == "sell")
        return buy_vol - sell_vol

    def divergence(self, price_change_pct: float, seconds: float = 30.0) -> float:
        """
        Measure CVD divergence from price.
        Returns: positive if CVD is bullish but price dropped (or vice versa).
        Large divergence = reversal signal.
        """
        delta = self.window_delta(seconds)
        total = sum(t.usd for t in self._ticks if time.time() - t.ts <= seconds)
        if total == 0:
            return 0.0
        # Normalize delta to -1..+1
        norm_delta = max(-1.0, min(1.0, delta / max(total * 0.5, 1.0)))
        # Normalize price change to -1..+1 (0.5% = full scale)
        norm_price = max(-1.0, min(1.0, price_change_pct / 0.5))
        # Divergence: delta says buy but price dropped
        return norm_delta - norm_price


# ---------------------------------------------------------------------------
# VWAP tracker
# ---------------------------------------------------------------------------

class VWAPTracker:
    """
    Volume-Weighted Average Price with standard deviation bands.

    Price below VWAP = sellers in control, look for mean reversion longs.
    Price above VWAP = buyers in control, look for mean reversion shorts.
    Distance from VWAP in terms of σ = overbought/oversold signal.
    """

    def __init__(self, window_seconds: float = 900.0) -> None:
        self._ticks: deque[tuple[float, float, float]] = deque(maxlen=100000)
        self._window = window_seconds

    def add_tick(self, price: float, volume: float, ts: float) -> None:
        self._ticks.append((ts, price, volume))

    @property
    def vwap(self) -> float:
        now = time.time()
        recent = [(p, v) for ts, p, v in self._ticks if now - ts <= self._window]
        if not recent:
            return 0.0
        total_vol = sum(v for _, v in recent)
        if total_vol <= 0:
            return recent[-1][0]  # fallback to last price
        return sum(p * v for p, v in recent) / total_vol

    @property
    def std_dev(self) -> float:
        """Price standard deviation weighted by volume around VWAP."""
        now = time.time()
        recent = [(p, v) for ts, p, v in self._ticks if now - ts <= self._window]
        if len(recent) < 10:
            return 0.0
        vw = self.vwap
        total_vol = sum(v for _, v in recent)
        if total_vol <= 0:
            return 0.0
        variance = sum(v * (p - vw) ** 2 for p, v in recent) / total_vol
        return math.sqrt(variance)

    def z_score(self, current_price: float) -> float:
        """How many σ is current price from VWAP."""
        vw = self.vwap
        sd = self.std_dev
        if sd <= 0 or vw <= 0:
            return 0.0
        return (current_price - vw) / sd

    def distance_pct(self, current_price: float) -> float:
        """Distance from VWAP as percentage of price."""
        vw = self.vwap
        if vw <= 0:
            return 0.0
        return (current_price - vw) / vw


# ---------------------------------------------------------------------------
# Realized volatility tracker
# ---------------------------------------------------------------------------

class VolatilityTracker:
    """
    Tracks realized volatility from tick data.
    Adapts thresholds to current market regime.
    """

    def __init__(self) -> None:
        # Store (timestamp, price) for return calculation
        self._prices_1s: deque[tuple[float, float]] = deque(maxlen=10000)
        self._returns_1m: deque[float] = deque(maxlen=500)
        self._last_1m_price: float = 0.0
        self._last_1m_ts: float = 0.0

    def add_price(self, price: float, ts: float) -> None:
        self._prices_1s.append((ts, price))
        # Compute 1-minute returns for vol estimation
        if self._last_1m_ts == 0:
            self._last_1m_price = price
            self._last_1m_ts = ts
        elif ts - self._last_1m_ts >= 60:
            if self._last_1m_price > 0:
                ret = math.log(price / self._last_1m_price)
                self._returns_1m.append(ret)
            self._last_1m_price = price
            self._last_1m_ts = ts

    def realized_vol_annualized(self) -> float:
        """Annualized realized vol from 1-minute returns."""
        if len(self._returns_1m) < 5:
            return 0.60  # default 60% annual vol for BTC
        mean = sum(self._returns_1m) / len(self._returns_1m)
        variance = sum((r - mean) ** 2 for r in self._returns_1m) / len(self._returns_1m)
        vol_per_min = math.sqrt(variance)
        # Annualize: sqrt(525600 minutes/year)
        return vol_per_min * math.sqrt(525600)

    def realized_vol_15m(self) -> float:
        """Expected 15-minute price move (1 std dev) as a percentage."""
        annual = self.realized_vol_annualized()
        # 15 min = 15/525600 of a year
        return annual * math.sqrt(15 / 525600)

    def price_momentum(self, seconds: float = 60.0) -> float:
        """Price change over last N seconds as percentage."""
        now = time.time()
        recent = [(ts, p) for ts, p in self._prices_1s if now - ts <= seconds]
        if len(recent) < 2:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / recent[0][1]

    def price_momentum_multi(self) -> dict[str, float]:
        """Price momentum at multiple timeframes."""
        return {
            "10s": self.price_momentum(10),
            "30s": self.price_momentum(30),
            "60s": self.price_momentum(60),
            "300s": self.price_momentum(300),
        }

    @property
    def regime(self) -> str:
        """Current volatility regime."""
        annual = self.realized_vol_annualized()
        if annual < 0.30:
            return "low"
        elif annual < 0.70:
            return "normal"
        elif annual < 1.20:
            return "high"
        else:
            return "extreme"

    def adaptive_threshold(self, base_threshold: float) -> float:
        """Scale a threshold by current vol regime."""
        regime_mult = {
            "low": 0.6,
            "normal": 1.0,
            "high": 1.5,
            "extreme": 2.5,
        }
        return base_threshold * regime_mult.get(self.regime, 1.0)


# ---------------------------------------------------------------------------
# Absorption detector
# ---------------------------------------------------------------------------

class AbsorptionDetector:
    """
    Detects passive order absorption.

    Absorption = large passive orders absorbing aggressive flow without
    price moving. This means a big player is accumulating/distributing.

    Example: Price hits $72,000, massive sell volume hits the bid,
    but price stays at $72,000. The bid is absorbing all selling.
    → Strong bullish signal (someone is buying everything).
    """

    def __init__(self) -> None:
        self._events: deque[dict] = deque(maxlen=500)

    def check(
        self,
        ticks: list[FlowTick],
        best_bid: float,
        best_ask: float,
        window_seconds: float = 5.0,
    ) -> float:
        """
        Returns absorption score: -1 (ask absorbing buys) to +1 (bid absorbing sells).
        Near 0 = no absorption detected.
        """
        now = time.time()
        recent = [t for t in ticks if now - t.ts <= window_seconds]
        if len(recent) < 10:
            return 0.0

        # Volume hitting the bid (aggressive sells) vs hitting the ask (aggressive buys)
        sell_vol_at_bid = sum(
            t.usd for t in recent
            if t.side == "sell" and abs(t.price - best_bid) / best_bid < 0.0002
        )
        buy_vol_at_ask = sum(
            t.usd for t in recent
            if t.side == "buy" and abs(t.price - best_ask) / best_ask < 0.0002
        )

        # Price stability during this volume
        if len(recent) >= 2:
            price_range = max(t.price for t in recent) - min(t.price for t in recent)
            mid = (best_bid + best_ask) / 2
            price_stability = 1.0 - min(price_range / (mid * 0.001), 1.0)
        else:
            price_stability = 0.0

        # Bid absorption: high sell volume hitting bid, but price stable
        bid_absorption = 0.0
        if sell_vol_at_bid > 50000 and price_stability > 0.6:
            bid_absorption = min(sell_vol_at_bid / 200000, 1.0) * price_stability

        # Ask absorption: high buy volume hitting ask, but price stable
        ask_absorption = 0.0
        if buy_vol_at_ask > 50000 and price_stability > 0.6:
            ask_absorption = min(buy_vol_at_ask / 200000, 1.0) * price_stability

        # Positive = bid absorbing (bullish), negative = ask absorbing (bearish)
        score = bid_absorption - ask_absorption

        if abs(score) > 0.3:
            self._events.append({
                "ts": now,
                "score": score,
                "sell_at_bid": sell_vol_at_bid,
                "buy_at_ask": buy_vol_at_ask,
                "stability": price_stability,
            })

        return round(max(-1.0, min(1.0, score)), 4)


# ---------------------------------------------------------------------------
# Sweep detector
# ---------------------------------------------------------------------------

class SweepDetector:
    """
    Detects aggressive sweeps through the order book.

    A sweep = a large order that eats through multiple price levels
    in rapid succession. This is institutional urgency.

    Buy sweep: consecutive fills at progressively HIGHER prices in < 500ms
    Sell sweep: consecutive fills at progressively LOWER prices in < 500ms
    """

    def __init__(self) -> None:
        self._events: deque[dict] = deque(maxlen=200)

    def check(self, ticks: list[FlowTick], window_ms: float = 500.0) -> float:
        """
        Returns sweep score: -1 (sell sweep) to +1 (buy sweep).
        Near 0 = no sweep detected.
        """
        now = time.time()
        window_s = window_ms / 1000.0
        recent = [t for t in ticks if now - t.ts <= window_s]

        if len(recent) < 5:
            return 0.0

        # Buy sweeps: consecutive fills at rising prices
        buy_sweep_vol = 0.0
        buy_levels = 0
        for i in range(1, len(recent)):
            if recent[i].side == "buy" and recent[i].price > recent[i - 1].price:
                buy_sweep_vol += recent[i].usd
                buy_levels += 1
            else:
                if buy_levels >= 3 and buy_sweep_vol > 25000:
                    break  # Found a sweep
                buy_sweep_vol = 0.0
                buy_levels = 0

        # Sell sweeps: consecutive fills at falling prices
        sell_sweep_vol = 0.0
        sell_levels = 0
        for i in range(1, len(recent)):
            if recent[i].side == "sell" and recent[i].price < recent[i - 1].price:
                sell_sweep_vol += recent[i].usd
                sell_levels += 1
            else:
                if sell_levels >= 3 and sell_sweep_vol > 25000:
                    break
                sell_sweep_vol = 0.0
                sell_levels = 0

        buy_score = min(buy_sweep_vol / 100000, 1.0) if buy_levels >= 3 else 0.0
        sell_score = min(sell_sweep_vol / 100000, 1.0) if sell_levels >= 3 else 0.0

        score = buy_score - sell_score

        if abs(score) > 0.2:
            self._events.append({
                "ts": now,
                "score": score,
                "buy_vol": buy_sweep_vol,
                "sell_vol": sell_sweep_vol,
                "buy_levels": buy_levels,
                "sell_levels": sell_levels,
            })

        return round(max(-1.0, min(1.0, score)), 4)


# ---------------------------------------------------------------------------
# Main flow analyzer — combines everything
# ---------------------------------------------------------------------------

class FlowAnalyzer:
    """
    The brain of the BTC scalping strategy.

    Processes every trade tick and order book update. Produces a FlowSignal
    that the bot uses as the PRIMARY trading signal.

    Signal hierarchy (weighted combination):
      1. CVD divergence from price     (25%) — strongest reversal signal
      2. Book pressure + absorption    (25%) — passive flow tells the truth
      3. Sweep detection               (15%) — institutional urgency
      4. VWAP mean reversion           (15%) — statistical edge
      5. Price momentum confirmation   (10%) — don't fight the trend
      6. Volume regime                 (10%) — adapt to conditions
    """

    # Weights for signal combination
    W_CVD = 0.25
    W_BOOK = 0.25
    W_SWEEP = 0.15
    W_VWAP = 0.15
    W_MOMENTUM = 0.10
    W_VOLUME = 0.10

    def __init__(self, asset: str = "BTC") -> None:
        self.asset = asset
        self.cvd = CVDTracker(window_seconds=300)
        self.vwap = VWAPTracker(window_seconds=900)
        self.vol = VolatilityTracker()
        self.absorption = AbsorptionDetector()
        self.sweep = SweepDetector()

        # Recent ticks for local analysis
        self._recent_ticks: deque[FlowTick] = deque(maxlen=10000)
        self._last_signal: Optional[FlowSignal] = None
        self._signal_count = 0

    # ------------------------------------------------------------------
    # Ingestion — called on every trade tick
    # ------------------------------------------------------------------

    def on_trade(self, price: float, qty: float, side: str, ts: float) -> None:
        """Process a single trade from Binance aggTrade stream."""
        usd = price * qty
        # Dynamic large-order threshold based on vol regime
        large_threshold = self.vol.adaptive_threshold(30000)
        tick = FlowTick(
            price=price, qty=qty, usd=usd,
            side=side, ts=ts, is_large=usd >= large_threshold,
        )
        self._recent_ticks.append(tick)
        self.cvd.add_tick(tick)
        self.vwap.add_tick(price, qty, ts)
        self.vol.add_price(price, ts)

    # ------------------------------------------------------------------
    # Signal computation — called on the analysis loop
    # ------------------------------------------------------------------

    def compute_signal(
        self, best_bid: float = 0.0, best_ask: float = 0.0, book_pressure: float = 0.0,
    ) -> FlowSignal:
        """
        Compute the composite flow signal.

        Call this every 100–500ms. It combines all sub-signals
        into a single directional signal with strength and confidence.
        """
        ticks = list(self._recent_ticks)
        if not ticks:
            return FlowSignal(
                direction="neutral", strength=0.0, confidence=0.0,
                signal_type="no_data", details="No trade data yet",
            )

        current_price = ticks[-1].price
        if current_price <= 0:
            return FlowSignal(
                direction="neutral", strength=0.0, confidence=0.0,
                signal_type="no_data", details="Invalid price",
            )

        # --- Compute sub-signals ---

        # 1. CVD
        cvd_slope = self.cvd.slope(seconds=10)
        # Normalize slope: $50k/sec buy pressure = 1.0
        cvd_norm = max(-1.0, min(1.0, cvd_slope / 50000))
        price_mom_30s = self.vol.price_momentum(30)
        cvd_div = self.cvd.divergence(price_mom_30s * 100, seconds=30)

        # 2. Absorption
        absorption = self.absorption.check(ticks, best_bid, best_ask, window_seconds=5.0)

        # 3. Sweeps
        sweep = self.sweep.check(ticks, window_ms=500)

        # 4. VWAP
        vwap_z = self.vwap.z_score(current_price)
        vwap_dist = self.vwap.distance_pct(current_price)

        # 5. Momentum
        mom = self.vol.price_momentum_multi()
        # Short-term momentum agreement
        mom_10 = mom["10s"]
        mom_60 = mom["60s"]

        # 6. Vol regime
        vol_annual = self.vol.realized_vol_annualized()
        regime = self.vol.regime

        # --- Weighted combination ---
        # Each sub-signal produces a directional score in [-1, +1]
        # Positive = bullish, negative = bearish

        # CVD component: slope + divergence
        cvd_component = cvd_norm * 0.6 + max(-1, min(1, cvd_div * 0.3)) * 0.4

        # Book component: absorption + book pressure from orderbook
        book_component = absorption * 0.6 + max(-1, min(1, book_pressure)) * 0.4

        # Sweep component
        sweep_component = sweep

        # VWAP component: mean reversion
        # If price is 2σ above VWAP and CVD is weakening, short signal
        # If price is 2σ below VWAP and CVD is strengthening, long signal
        if vwap_z > 2.0 and cvd_norm < 0:
            vwap_component = -min(abs(vwap_z) / 3.0, 1.0)
        elif vwap_z < -2.0 and cvd_norm > 0:
            vwap_component = min(abs(vwap_z) / 3.0, 1.0)
        elif abs(vwap_z) > 1.5:
            vwap_component = -max(-1, min(1, vwap_z / 3.0)) * 0.5
        else:
            vwap_component = 0.0

        # Momentum component
        if abs(mom_10) > 0.0005:  # > 0.05% in 10 seconds is significant
            mom_component = max(-1, min(1, mom_10 / 0.003))
        else:
            mom_component = 0.0

        # Volume component: high vol = stronger signals, low vol = weaker
        vol_mult = {"low": 0.5, "normal": 1.0, "high": 1.2, "extreme": 0.8}
        vol_component = vol_mult.get(regime, 1.0)

        # Weighted sum
        raw_score = (
            cvd_component * self.W_CVD
            + book_component * self.W_BOOK
            + sweep_component * self.W_SWEEP
            + vwap_component * self.W_VWAP
            + mom_component * self.W_MOMENTUM
        )

        # Apply vol regime multiplier (extreme vol reduces confidence, not direction)
        direction_score = max(-1.0, min(1.0, raw_score))

        # --- Direction ---
        if direction_score > 0.10:
            direction = "long"
        elif direction_score < -0.10:
            direction = "short"
        else:
            direction = "neutral"

        # --- Strength ---
        strength = min(abs(direction_score) / 0.6, 1.0)

        # --- Confidence ---
        # Higher when multiple signals agree
        signals_agreeing = 0
        signal_dirs = [cvd_component, book_component, sweep_component, mom_component]
        if direction == "long":
            signals_agreeing = sum(1 for s in signal_dirs if s > 0.05)
        elif direction == "short":
            signals_agreeing = sum(1 for s in signal_dirs if s < -0.05)

        agreement_bonus = signals_agreeing / len(signal_dirs)
        vol_confidence = vol_component if regime != "extreme" else 0.4
        confidence = min(1.0, (agreement_bonus * 0.6 + vol_confidence * 0.2 + strength * 0.2))

        # Reduce confidence in extreme vol (unpredictable)
        if regime == "extreme":
            confidence *= 0.6

        # --- Signal type ---
        components = {
            "cvd_divergence": abs(cvd_div),
            "absorption": abs(absorption),
            "sweep": abs(sweep),
            "vwap_reversion": abs(vwap_component),
            "momentum": abs(mom_component),
        }
        signal_type = max(components, key=components.get)

        # --- Details ---
        parts = []
        if abs(cvd_norm) > 0.2:
            parts.append(f"CVD {'buying' if cvd_norm > 0 else 'selling'} {abs(cvd_norm):.2f}")
        if abs(cvd_div) > 0.3:
            parts.append(f"CVD divergence {cvd_div:+.2f}")
        if abs(absorption) > 0.2:
            parts.append(f"absorption {'bid' if absorption > 0 else 'ask'} {abs(absorption):.2f}")
        if abs(sweep) > 0.2:
            parts.append(f"sweep {'buy' if sweep > 0 else 'sell'} {abs(sweep):.2f}")
        if abs(vwap_z) > 1.5:
            parts.append(f"VWAP z={vwap_z:+.1f}")
        if abs(mom_10) > 0.001:
            parts.append(f"mom10s={mom_10*100:+.3f}%")
        parts.append(f"vol={regime}")
        details = " | ".join(parts) if parts else "weak signals"

        signal = FlowSignal(
            direction=direction,
            strength=round(strength, 4),
            confidence=round(confidence, 4),
            signal_type=signal_type,
            details=details,
            cvd_slope=round(cvd_slope, 2),
            cvd_divergence=round(cvd_div, 4),
            vwap_distance_pct=round(vwap_dist * 100, 4),
            absorption_score=absorption,
            sweep_score=sweep,
            book_pressure=round(book_pressure, 4),
            realized_vol=round(vol_annual, 4),
            vol_regime=regime,
            price_momentum=round(mom_10 * 100, 4),
        )

        self._last_signal = signal
        self._signal_count += 1
        return signal

    def get_scalp_decision(
        self,
        current_price: float,
        best_bid: float,
        best_ask: float,
        book_pressure: float,
        has_position: bool = False,
        position_side: str = "",
    ) -> ScalpDecision:
        """
        Produce a final scalp decision for the bot to act on.
        """
        signal = self.compute_signal(best_bid, best_ask, book_pressure)
        vol_15m = self.vol.realized_vol_15m()

        # No signal → hold
        if not signal.is_actionable:
            if has_position:
                # Check if flow is reversing against our position
                if position_side == "long" and signal.direction == "short" and signal.strength > 0.4:
                    return ScalpDecision(
                        action="exit", asset=self.asset,
                        confidence=signal.strength,
                        size_fraction=0.0,
                        reason=f"Flow reversing against long: {signal.details}",
                        signal=signal,
                    )
                elif position_side == "short" and signal.direction == "long" and signal.strength > 0.4:
                    return ScalpDecision(
                        action="exit", asset=self.asset,
                        confidence=signal.strength,
                        size_fraction=0.0,
                        reason=f"Flow reversing against short: {signal.details}",
                        signal=signal,
                    )
            return ScalpDecision(
                action="hold", asset=self.asset,
                confidence=0.0, size_fraction=0.0,
                reason=f"No actionable signal: {signal.details}",
                signal=signal,
            )

        # If we already have a position in the same direction → hold
        if has_position and position_side == signal.direction.replace("long", "long").replace("short", "short"):
            return ScalpDecision(
                action="hold", asset=self.asset,
                confidence=signal.confidence,
                size_fraction=0.0,
                reason=f"Already positioned {position_side}, signal confirms",
                signal=signal,
            )

        # If we have a position in the OPPOSITE direction → exit first
        if has_position:
            return ScalpDecision(
                action="exit", asset=self.asset,
                confidence=signal.confidence,
                size_fraction=0.0,
                reason=f"Flow reversed to {signal.direction}: {signal.details}",
                signal=signal,
            )

        # --- New entry ---
        # Size: scale with signal strength and confidence
        size_fraction = signal.strength * signal.confidence

        # Stop loss: 1.5x the expected 15-min move
        stop_distance = max(current_price * vol_15m * 1.5, current_price * 0.002)
        # Target: 2x the expected 15-min move (2:1 reward/risk)
        target_distance = max(current_price * vol_15m * 2.0, current_price * 0.003)

        if signal.direction == "long":
            stop_price = current_price - stop_distance
            target_price = current_price + target_distance
        else:
            stop_price = current_price + stop_distance
            target_price = current_price - target_distance

        return ScalpDecision(
            action=f"enter_{signal.direction}",
            asset=self.asset,
            confidence=signal.confidence,
            size_fraction=round(size_fraction, 4),
            reason=f"{signal.signal_type}: {signal.details}",
            entry_price=current_price,
            stop_price=round(stop_price, 2),
            target_price=round(target_price, 2),
            signal=signal,
        )

    @property
    def last_signal(self) -> Optional[FlowSignal]:
        return self._last_signal
