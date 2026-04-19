"""
Threshold optimiser.

Inputs:
    - rest-of-day forecast prices (general + feed-in), $/kWh
    - current battery SoC, %
    - battery capacity, kWh
    - expected PV remaining today, kWh  (from Solcast)
    - expected load remaining today, kWh

Output:
    Dict of the 8 input_number values to write back to HA.

Strategy (first pass — percentile-based with reserve scaled by surplus):
    sell_price_threshold      = P(sell_high_pct) of feed-in prices
    sell_price_low_threshold  = P(sell_low_pct)  of feed-in prices
    buy_price_low_battery     = P(buy_low_pct)   of general prices   (emergency charge only when very cheap)
    buy_price_mid_battery     = P(buy_mid_pct)   of general prices

    Reserves flex with PV surplus:
      surplus_ratio = (pv_remaining - load_remaining) / battery_capacity_kwh
      - big +ve surplus  → drain aggressively, low reserves
      - near zero / -ve  → protect evening reserves
"""
from __future__ import annotations

from dataclasses import dataclass


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class OptimiserInputs:
    general_prices: list[float]   # $/kWh, rest of day
    feed_in_prices: list[float]   # $/kWh, rest of day
    current_soc_pct: float
    battery_capacity_kwh: float
    soc_floor_pct: float
    soc_ceiling_pct: float
    pv_remaining_kwh: float
    load_remaining_kwh: float
    sell_high_pct: int
    sell_low_pct: int
    buy_low_pct: int
    buy_mid_pct: int
    # Hard guardrails — applied *after* the percentile maths.
    min_sell_soc_pct: float       # never sell below this SoC
    max_buy_soc_pct: float        # never buy above this SoC
    sell_price_floor: float       # $/kWh — never sell below this feed-in price
    # Option A — tomorrow's forecast, blended by today_weight (1.0 at dawn → 0.0 at dusk).
    pv_tomorrow_kwh: float
    load_tomorrow_kwh: float
    today_weight: float           # 0..1


def compute(inp: OptimiserInputs) -> dict[str, float]:
    # --- price thresholds (percentiles of remaining forecast) ---------------
    sell_high = _percentile(inp.feed_in_prices, inp.sell_high_pct)
    sell_low  = _percentile(inp.feed_in_prices, inp.sell_low_pct)
    buy_low   = _percentile(inp.general_prices, inp.buy_low_pct)
    buy_mid   = _percentile(inp.general_prices, inp.buy_mid_pct)

    # Guarantee ordering even if the distribution is flat (offsets in $/kWh — 0.01 = 1 c/kWh).
    if sell_low >= sell_high:
        sell_low = sell_high - 0.01
    if buy_mid  <= buy_low:
        buy_mid  = buy_low + 0.01

    # Price floor: never sell below this, regardless of forecast shape.
    sell_high = max(sell_high, inp.sell_price_floor)
    sell_low  = max(sell_low,  inp.sell_price_floor)
    # After flooring they may collide — nudge high back above low.
    if sell_high <= sell_low:
        sell_high = sell_low + 0.01

    # --- SoC reserves (scale with PV surplus over the relevant horizon) -----
    # Blend today vs tomorrow based on how much daylight is still ahead today.
    # At dawn today_weight=1 (decisions driven by today); after dusk today_weight→0
    # (decisions driven by tomorrow, since nothing's coming from today anymore).
    tw = _clamp(inp.today_weight, 0.0, 1.0)
    effective_pv   = tw * inp.pv_remaining_kwh   + (1 - tw) * inp.pv_tomorrow_kwh
    effective_load = tw * inp.load_remaining_kwh + (1 - tw) * inp.load_tomorrow_kwh
    surplus = effective_pv - effective_load
    surplus_ratio = surplus / max(inp.battery_capacity_kwh, 0.1)
    # ratio in ~[-2, +2] typically. Map to a drain-aggressiveness in [0, 1].
    aggression = _clamp((surplus_ratio + 1.0) / 3.0, 0.0, 1.0)

    # High-price sell allowed to drain close to the floor (but never below min_sell_soc_pct).
    sell_battery_minimum     = _clamp(
        inp.soc_floor_pct + (1 - aggression) * 15.0,
        max(inp.soc_floor_pct, inp.min_sell_soc_pct), inp.soc_ceiling_pct
    )
    # Mid-price sell keeps more reserve for the evening peak.
    sell_low_battery_minimum = _clamp(
        sell_battery_minimum + 15.0 + (1 - aggression) * 10.0,
        max(inp.soc_floor_pct, inp.min_sell_soc_pct), inp.soc_ceiling_pct
    )

    # Emergency charge band: buy aggressively only when battery is low.
    buy_battery_low_threshold  = _clamp(inp.soc_floor_pct + 10.0, 5.0, 40.0)

    # Opportunistic charge band top — direct model: "grid should top us up
    # only to the point where today's solar can take the rest." Anything above
    # that is paying for kWh the sun would have delivered free.
    #
    # expected_pv_fill_pct: %-points that today's solar (minus today's load)
    # is expected to add to the battery. Negative on dull days.
    #
    # Using TODAY's PV only here (not the blended value) — tomorrow's sun can't
    # fill today's battery.
    expected_pv_fill_pct = (
        (inp.pv_remaining_kwh - inp.load_remaining_kwh) /
        max(inp.battery_capacity_kwh, 0.1)
    ) * 100.0
    buy_battery_high_threshold = _clamp(
        100.0 - expected_pv_fill_pct,
        buy_battery_low_threshold + 10.0, inp.max_buy_soc_pct
    )

    return {
        # Prices are $/kWh → 5 decimal places preserves sub-cent precision
        "sell_price_threshold":       round(sell_high, 5),
        "sell_price_low_threshold":   round(sell_low, 5),
        "buy_price_low_battery":      round(buy_low, 5),
        "buy_price_mid_battery":      round(buy_mid, 5),
        # SoC values are percentages
        "sell_battery_minimum":       round(sell_battery_minimum, 1),
        "sell_low_battery_minimum":   round(sell_low_battery_minimum, 1),
        "buy_battery_low_threshold":  round(buy_battery_low_threshold, 1),
        "buy_battery_high_threshold": round(buy_battery_high_threshold, 1),
    }
