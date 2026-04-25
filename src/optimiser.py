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

Buy-mid strategy (price-scan):
    Target: battery peaks at buy_target_soc_pct (default 85%).
    1. Estimate kWh still needed from the grid:
         needed = target_kwh - current_kwh - max(0, pv_remaining - load_remaining)
    2. Scan unique general prices low → high (up to buy_max_price $/kWh).
       At each candidate price P, the battery would charge during every interval
       where price ≤ P:
         potential = count(intervals ≤ P) × charge_rate_kw × 0.5h
    3. First P where potential ≥ needed becomes buy_price_mid_battery.
       If needed ≤ 0, no mid-band buying is required (price set to 0).

Sell strategy (unchanged — percentile-based):
    sell_price_threshold     = P(sell_high_pct) of feed-in prices
    sell_price_low_threshold = P(sell_low_pct)  of feed-in prices

Emergency buy (unchanged — percentile-based):
    buy_price_low_battery    = P(buy_low_pct) of general prices
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


def _scan_buy_mid_price(
    general_prices: list[float],
    current_soc_pct: float,
    pv_remaining_kwh: float,
    load_remaining_kwh: float,
    battery_capacity_kwh: float,
    battery_charge_rate_kw: float,
    buy_target_soc_pct: float,
    buy_max_price: float,           # $/kWh ceiling
) -> float:
    """Return the lowest buy price that, if set as the mid-band limit, would
    purchase enough grid energy to bring the battery to buy_target_soc_pct."""
    target_kwh  = buy_target_soc_pct / 100.0 * battery_capacity_kwh
    current_kwh = current_soc_pct   / 100.0 * battery_capacity_kwh
    solar_net   = max(0.0, pv_remaining_kwh - load_remaining_kwh)
    needed_kwh  = max(0.0, target_kwh - current_kwh - solar_net)

    if needed_kwh <= 0.0:
        # Use a large negative sentinel so the HA controller's price comparison
        # (current_price <= threshold) is never satisfied — safer than 0.0 which
        # some automations might treat as "unconstrained".
        return -9.99

    energy_per_interval = battery_charge_rate_kw * 0.5  # kWh per 30-min slot

    # Unique candidate thresholds from the actual price list, capped at max.
    candidates = sorted({p for p in general_prices if p <= buy_max_price})

    for price in candidates:
        count     = sum(1 for p in general_prices if p <= price)
        potential = count * energy_per_interval
        if potential >= needed_kwh:
            return price

    # Even buying at every sub-max interval isn't enough — accept buy_max_price.
    if candidates:
        return buy_max_price

    return -9.99  # no forecast prices below the ceiling — don't buy mid-band


@dataclass(frozen=True)
class OptimiserInputs:
    general_prices: list[float]     # $/kWh, rest of day
    feed_in_prices: list[float]     # $/kWh, rest of day
    current_soc_pct: float
    battery_capacity_kwh: float
    battery_charge_rate_kw: float
    soc_floor_pct: float
    soc_ceiling_pct: float
    pv_remaining_kwh: float
    load_remaining_kwh: float
    sell_high_pct: int
    sell_low_pct: int
    buy_low_pct: int
    buy_target_soc_pct: float       # target SoC for mid-band grid charging
    buy_max_price: float            # $/kWh ceiling for mid-band buying
    # Hard guardrails — applied *after* the main maths.
    min_sell_soc_pct: float         # never sell below this SoC
    max_buy_soc_pct: float          # never buy above this SoC
    sell_price_floor: float         # $/kWh — never sell below this feed-in price
    # Option A — tomorrow's forecast, blended by today_weight (1.0 at dawn → 0.0 at dusk).
    pv_tomorrow_kwh: float
    load_tomorrow_kwh: float
    today_weight: float             # 0..1


def compute(inp: OptimiserInputs) -> dict[str, float]:
    # --- sell price thresholds (percentiles of remaining feed-in forecast) ----
    sell_high = _percentile(inp.feed_in_prices, inp.sell_high_pct)
    sell_low  = _percentile(inp.feed_in_prices, inp.sell_low_pct)

    if sell_low >= sell_high:
        sell_low = sell_high - 0.01

    sell_high = max(sell_high, inp.sell_price_floor)
    sell_low  = max(sell_low,  inp.sell_price_floor)
    if sell_high <= sell_low:
        sell_high = sell_low + 0.01

    # --- emergency buy price (percentile-based, for critically-low battery) --
    buy_low = _percentile(inp.general_prices, inp.buy_low_pct)

    # --- mid buy price (price-scan: cheapest price that covers our deficit) --
    buy_mid = _scan_buy_mid_price(
        general_prices=inp.general_prices,
        current_soc_pct=inp.current_soc_pct,
        pv_remaining_kwh=inp.pv_remaining_kwh,
        load_remaining_kwh=inp.load_remaining_kwh,
        battery_capacity_kwh=inp.battery_capacity_kwh,
        battery_charge_rate_kw=inp.battery_charge_rate_kw,
        buy_target_soc_pct=inp.buy_target_soc_pct,
        buy_max_price=inp.buy_max_price,
    )

    if buy_mid <= buy_low:
        buy_mid = buy_low + 0.01

    # --- SoC reserves (scale with PV surplus over the relevant horizon) -----
    tw = _clamp(inp.today_weight, 0.0, 1.0)
    effective_pv   = tw * inp.pv_remaining_kwh   + (1 - tw) * inp.pv_tomorrow_kwh
    effective_load = tw * inp.load_remaining_kwh + (1 - tw) * inp.load_tomorrow_kwh
    surplus        = effective_pv - effective_load
    surplus_ratio  = surplus / max(inp.battery_capacity_kwh, 0.1)
    aggression     = _clamp((surplus_ratio + 1.0) / 3.0, 0.0, 1.0)

    sell_battery_minimum = _clamp(
        inp.soc_floor_pct + (1 - aggression) * 15.0,
        max(inp.soc_floor_pct, inp.min_sell_soc_pct), inp.soc_ceiling_pct,
    )
    sell_low_battery_minimum = _clamp(
        sell_battery_minimum + 15.0 + (1 - aggression) * 10.0,
        max(inp.soc_floor_pct, inp.min_sell_soc_pct), inp.soc_ceiling_pct,
    )

    # Emergency charge band — only when battery is very low.
    buy_battery_low_threshold = _clamp(inp.soc_floor_pct + 10.0, 5.0, 40.0)

    # Mid-band charge ceiling — the SoC we're targeting.
    buy_battery_high_threshold = _clamp(
        inp.buy_target_soc_pct,
        buy_battery_low_threshold + 10.0, inp.max_buy_soc_pct,
    )

    return {
        "sell_price_threshold":       round(sell_high, 5),
        "sell_price_low_threshold":   round(sell_low, 5),
        "buy_price_low_battery":      round(buy_low, 5),
        "buy_price_mid_battery":      round(buy_mid, 5),
        "sell_battery_minimum":       round(sell_battery_minimum, 1),
        "sell_low_battery_minimum":   round(sell_low_battery_minimum, 1),
        "buy_battery_low_threshold":  round(buy_battery_low_threshold, 1),
        "buy_battery_high_threshold": round(buy_battery_high_threshold, 1),
    }
