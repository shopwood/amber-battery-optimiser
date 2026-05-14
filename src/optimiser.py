"""
Threshold optimiser.

Inputs:
    - rest-of-day forecast prices (general + feed-in), $/kWh
    - current battery SoC, %
    - battery capacity, kWh
    - expected PV remaining today, kWh  (from Solcast)
    - expected load remaining today, kWh

Output:
    Dict of the 7 input_number values to write back to HA.

Buy-mid strategy (price-scan):
    Target: battery peaks at buy_target_soc_pct (default 85%).
    1. Estimate kWh still needed from the grid:
         needed = target_kwh - current_kwh - max(0, pv_remaining - load_remaining)
    2. Scan unique general prices low → high (up to buy_max_price $/kWh).
       At each candidate price P, the battery would charge during every interval
       where price ≤ P:
         potential = count(intervals ≤ P) × charge_rate_kw × 0.5h
    3. First P where potential ≥ needed becomes buy_price_mid_battery.
       If needed ≤ 0, no mid-band buying is required (price set to sentinel).

Sell strategy (price-scan, mirroring buy):
    Available: surplus above sell_min_soc_pct floor + net PV remaining.
    1. Estimate kWh available to sell:
         available = (current_soc - sell_min_soc) × capacity + max(0, pv - load)
    2. Scan unique feed-in prices high → low (above sell_price_floor and buy_mid + 4c).
       At each candidate price P, the battery would sell during every interval
       where price ≥ P:
         potential = count(intervals ≥ P) × charge_rate_kw × 0.5h
    3. First P (from top) where potential ≥ available becomes sell_price_threshold.
       If available ≤ 0, no normal selling (price set to sentinel).

Spike sell:
    Absolute price override — sell down to soc_floor regardless of sell_battery_minimum.
    Configured via sell_spike_price ($/kWh); always active above that price.

Emergency buy (percentile-based):
    buy_price_low_battery = P(buy_low_pct) of general prices
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


def _scan_sell_price(
    feed_in_prices: list[float],
    current_soc_pct: float,
    pv_remaining_kwh: float,
    load_remaining_kwh: float,
    pv_tomorrow_kwh: float,
    load_tomorrow_kwh: float,
    battery_capacity_kwh: float,
    battery_charge_rate_kw: float,
    sell_min_soc_pct: float,        # % — don't sell below this SoC
    sell_price_floor: float,        # $/kWh absolute minimum sell price
    buy_price_mid: float,           # $/kWh — sell must be >= this + 0.04
) -> float:
    """Return the highest sell price threshold that still captures enough
    expensive intervals to sell all available surplus energy."""
    floor_kwh     = sell_min_soc_pct / 100.0 * battery_capacity_kwh
    current_kwh   = current_soc_pct  / 100.0 * battery_capacity_kwh
    solar_net     = max(0.0, pv_remaining_kwh - load_remaining_kwh)
    # Reserve energy to cover tomorrow's expected grid shortfall on a cloudy day.
    tomorrow_deficit = max(0.0, load_tomorrow_kwh - pv_tomorrow_kwh)
    available_kwh = max(0.0, current_kwh - floor_kwh + solar_net - tomorrow_deficit)

    if available_kwh <= 0.0:
        return 9.99  # nothing to sell — sentinel that never triggers

    energy_per_interval = battery_charge_rate_kw * 0.5  # kWh per 30-min slot

    # Combined price floor: must clear sell_price_floor AND stay 4c above buy_mid.
    min_price = max(sell_price_floor, buy_price_mid + 0.04)

    # Unique candidate thresholds above the floor, scanned high → low.
    candidates = sorted({p for p in feed_in_prices if p >= min_price}, reverse=True)

    for price in candidates:
        count     = sum(1 for p in feed_in_prices if p >= price)
        potential = count * energy_per_interval
        if potential >= available_kwh:
            return price

    # Even at the lowest qualifying price we can't cover available_kwh —
    # use min_price to capture every qualifying interval.
    if candidates:
        return min_price

    return 9.99  # no forecast prices above the floor — don't sell normally


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
    pv_tomorrow_kwh: float
    load_tomorrow_kwh: float
    sell_spike_price: float          # $/kWh — sell at floor SoC when above this price
    buy_low_pct: int
    buy_target_soc_pct: float       # target SoC for mid-band grid charging
    buy_max_price: float            # $/kWh ceiling for mid-band buying
    # Hard guardrails — applied *after* the main maths.
    min_sell_soc_pct: float         # never sell below this SoC
    max_buy_soc_pct: float          # never buy above this SoC
    sell_price_floor: float         # $/kWh — never sell below this feed-in price


def compute(inp: OptimiserInputs) -> dict[str, float]:
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

    # Guard only for the no-buy sentinel (-9.99); a scan result that legitimately
    # equals buy_low is fine — the two thresholds use different SoC gates in HA.
    if buy_mid < 0:
        buy_mid = buy_low + 0.01

    # --- sell price (price-scan: highest threshold covering surplus kWh) ------
    sell_price = _scan_sell_price(
        feed_in_prices=inp.feed_in_prices,
        current_soc_pct=inp.current_soc_pct,
        pv_remaining_kwh=inp.pv_remaining_kwh,
        load_remaining_kwh=inp.load_remaining_kwh,
        pv_tomorrow_kwh=inp.pv_tomorrow_kwh,
        load_tomorrow_kwh=inp.load_tomorrow_kwh,
        battery_capacity_kwh=inp.battery_capacity_kwh,
        battery_charge_rate_kw=inp.battery_charge_rate_kw,
        sell_min_soc_pct=inp.min_sell_soc_pct,
        sell_price_floor=inp.sell_price_floor,
        buy_price_mid=buy_mid,
    )

    # --- spike sell: absolute price override (ignores SoC minimum) ------------
    sell_spike = max(inp.sell_spike_price, inp.sell_price_floor)

    # --- SoC thresholds -------------------------------------------------------
    sell_battery_minimum = inp.min_sell_soc_pct

    # Emergency charge band — only when battery is very low.
    buy_battery_low_threshold = _clamp(inp.soc_floor_pct + 10.0, 5.0, 40.0)

    # Mid-band charge ceiling — the SoC we're targeting.
    buy_battery_high_threshold = _clamp(
        inp.buy_target_soc_pct,
        buy_battery_low_threshold + 10.0, inp.max_buy_soc_pct,
    )

    return {
        "sell_price_threshold":       round(sell_price, 5),
        "sell_spike_price_threshold": round(sell_spike, 5),
        "sell_battery_minimum":       round(sell_battery_minimum, 1),
        "buy_price_low_battery":      round(buy_low, 5),
        "buy_price_mid_battery":      round(buy_mid, 5),
        "buy_battery_low_threshold":  round(buy_battery_low_threshold, 1),
        "buy_battery_high_threshold": round(buy_battery_high_threshold, 1),
    }
