"""
Threshold optimiser.

Inputs:
    - rest-of-day forecast prices (general + feed-in), $/kWh
    - current battery SoC, %
    - battery capacity, kWh
    - expected PV remaining today, kWh  (from Solcast)
    - expected load remaining today, kWh

Output:
    Dict of the 4 buy-side input_number values to write back to HA.

Buy-mid strategy (price-scan with dynamic survival target):

    Target is computed dynamically, max of:
      - Static baseline: buy_target_soc_pct × capacity   (e.g. 85%, summer-friendly)
      - Survival reserve: floor + today's net deficit + tomorrow's net deficit
        where deficit_X = max(0, load_X - pv_X)
    …capped at max_buy_soc_pct × capacity.

    In summer (PV ≥ load) the deficits are zero and the static baseline dominates
    — behaviour unchanged. In winter (PV < load) the survival reserve exceeds the
    baseline and pushes the target up toward the max_buy cap, ensuring the battery
    is full enough at end of day to cover the overnight + next-day gap without
    falling to the floor.

    Scan procedure:
    1. needed = max(0, target_kwh - current_kwh - max(0, pv_remaining - load_remaining))
    2. Bound the planning horizon at the next price spike (any interval ≥
       2× the rest-of-day median). We can't defer charging across a peak —
       the battery has to ride it out from whatever SoC it has when it hits.
       Without this bound the scan would pick the cheapest post-peak intervals
       and leave the battery underfilled going in.
    3. Scan unique pre-spike prices low → high (up to buy_max_price).
       At each candidate price P, count pre-spike intervals where price ≤ P:
         potential = count × charge_rate_kw × 0.5h
    4. First P where potential ≥ needed becomes buy_price_mid_battery.
       If pre-spike capacity can't cover the deficit, threshold = buy_max_price
       (buy at every qualifying pre-spike interval). If the current interval
       IS the spike, no mid-band buying — emergency band still applies.

Emergency buy (percentile-based):
    buy_price_low_battery = P(buy_low_pct) of general prices

Sell side:
    Removed from the optimiser — sell decisions are now driven manually in HA
    via the Min Sell Price / Min Battery to Sell / sell_spike_price_threshold
    helpers. The feed-in prices and tomorrow's-forecast inputs are still accepted
    (plumbing kept) but no longer consumed here.
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


# Spike detection: any interval whose price is at least this multiple of the
# rest-of-day median is treated as a peak we shouldn't try to charge across.
# 2.0 catches Australia's typical evening peak (e.g. 60c+ on a 22c median day)
# without flagging normal shoulder hours.
_SPIKE_FACTOR = 2.0


def _next_spike_index(prices: list[float]) -> int | None:
    """Index of the first chronological interval that's a 'spike' relative to
    the rest-of-day distribution. Returns None if no interval qualifies.

    Used to bound the buy-scan horizon: don't plan grid charging across a peak,
    because the battery will be drained / opportunity-cost-burned during it.
    The cheapest intervals on a typical Amber day cluster overnight, AFTER the
    evening peak — without this check the scan picks them and leaves the
    battery low going INTO the peak.
    """
    if not prices:
        return None
    sorted_p = sorted(prices)
    median = sorted_p[len(sorted_p) // 2]
    threshold = median * _SPIKE_FACTOR
    for i, p in enumerate(prices):
        if p >= threshold:
            return i
    return None


def _compute_target_kwh(
    buy_target_soc_pct: float,     # static baseline target (%)
    max_buy_soc_pct: float,        # hard cap on dynamic target (%)
    soc_floor_pct: float,
    battery_capacity_kwh: float,
    load_remaining_kwh: float,
    load_tomorrow_kwh: float,
    pv_remaining_kwh: float,
    pv_tomorrow_kwh: float,
) -> float:
    """Dynamic target kWh for grid charging.

    Returns max(static target, survival reserve), capped at max_buy_soc_pct.

    Survival reserve = floor + today's net load deficit + tomorrow's net load
    deficit, where deficit_X = max(0, load_X - pv_X). In summer (PV surplus)
    deficits are 0 and the static baseline dominates — same behaviour as
    before. In winter (PV < load) the survival reserve exceeds the baseline
    and the optimiser plans to charge the battery higher than its baseline,
    up to the max_buy cap. Push max_buy_soc_pct toward the SoC ceiling
    (e.g. 95) to allow full winter top-up.
    """
    static = buy_target_soc_pct / 100.0 * battery_capacity_kwh
    floor  = soc_floor_pct      / 100.0 * battery_capacity_kwh
    cap    = max_buy_soc_pct    / 100.0 * battery_capacity_kwh

    today_deficit    = max(0.0, load_remaining_kwh - pv_remaining_kwh)
    tomorrow_deficit = max(0.0, load_tomorrow_kwh  - pv_tomorrow_kwh)
    survival = floor + today_deficit + tomorrow_deficit

    return min(cap, max(static, survival))


def _scan_buy_mid_price(
    general_prices: list[float],
    current_soc_pct: float,
    pv_remaining_kwh: float,
    load_remaining_kwh: float,
    battery_capacity_kwh: float,
    battery_charge_rate_kw: float,
    target_kwh: float,              # kWh target (precomputed by _compute_target_kwh)
    buy_max_price: float,           # $/kWh ceiling
) -> float:
    """Return the lowest buy price that, if set as the mid-band limit, would
    purchase enough grid energy to bring the battery to target_kwh."""
    current_kwh = current_soc_pct   / 100.0 * battery_capacity_kwh
    solar_net   = max(0.0, pv_remaining_kwh - load_remaining_kwh)
    needed_kwh  = max(0.0, target_kwh - current_kwh - solar_net)

    if needed_kwh <= 0.0:
        # Use a large negative sentinel so the HA controller's price comparison
        # (current_price <= threshold) is never satisfied — safer than 0.0 which
        # some automations might treat as "unconstrained".
        return -9.99

    # Constrain the scan to intervals BEFORE the next price spike. We can't
    # defer charging across a peak — the battery has to ride out the spike from
    # whatever charge it has when the spike hits. If the cheapest intervals are
    # post-peak (e.g. overnight after a 3–9pm peak), the scan would otherwise
    # set a threshold so low that nothing fires before the peak, leaving the
    # battery underfilled going in.
    spike_idx = _next_spike_index(general_prices)
    horizon = general_prices[:spike_idx] if spike_idx is not None else general_prices

    if not horizon:
        # Current interval is itself the spike — no pre-spike buying possible.
        # Emergency-band buy (separate threshold) still applies if SoC is low.
        return -9.99

    energy_per_interval = battery_charge_rate_kw * 0.5  # kWh per 30-min slot

    # Unique candidate thresholds from the pre-spike list, capped at max.
    candidates = sorted({p for p in horizon if p <= buy_max_price})

    for price in candidates:
        count     = sum(1 for p in horizon if p <= price)
        potential = count * energy_per_interval
        if potential >= needed_kwh:
            return price

    # Pre-spike capacity can't cover the deficit — accept buy_max_price so the
    # automation buys at every qualifying pre-spike interval (best we can do
    # before the peak forces us to ride it out from current SoC).
    if candidates:
        return buy_max_price

    return -9.99  # no pre-spike prices below the ceiling — don't buy mid-band


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

    # --- dynamic target: static baseline OR survival reserve, whichever's higher.
    target_kwh = _compute_target_kwh(
        buy_target_soc_pct=inp.buy_target_soc_pct,
        max_buy_soc_pct=inp.max_buy_soc_pct,
        soc_floor_pct=inp.soc_floor_pct,
        battery_capacity_kwh=inp.battery_capacity_kwh,
        load_remaining_kwh=inp.load_remaining_kwh,
        load_tomorrow_kwh=inp.load_tomorrow_kwh,
        pv_remaining_kwh=inp.pv_remaining_kwh,
        pv_tomorrow_kwh=inp.pv_tomorrow_kwh,
    )

    # --- mid buy price (price-scan: cheapest price that covers our deficit) --
    buy_mid = _scan_buy_mid_price(
        general_prices=inp.general_prices,
        current_soc_pct=inp.current_soc_pct,
        pv_remaining_kwh=inp.pv_remaining_kwh,
        load_remaining_kwh=inp.load_remaining_kwh,
        battery_capacity_kwh=inp.battery_capacity_kwh,
        battery_charge_rate_kw=inp.battery_charge_rate_kw,
        target_kwh=target_kwh,
        buy_max_price=inp.buy_max_price,
    )

    # Guard only for the no-buy sentinel (-9.99); a scan result that legitimately
    # equals buy_low is fine — the two thresholds use different SoC gates in HA.
    if buy_mid < 0:
        buy_mid = buy_low + 0.01

    # Emergency charge band — only when battery is very low.
    buy_battery_low_threshold = _clamp(inp.soc_floor_pct + 10.0, 5.0, 40.0)

    # Mid-band charge ceiling — reflects the dynamic target (the SoC at which
    # the HA automation stops grid-charging in the mid band).
    dynamic_target_pct = (target_kwh / max(inp.battery_capacity_kwh, 0.1)) * 100.0
    buy_battery_high_threshold = _clamp(
        dynamic_target_pct,
        buy_battery_low_threshold + 10.0, inp.max_buy_soc_pct,
    )

    return {
        "buy_price_low_battery":      round(buy_low, 5),
        "buy_price_mid_battery":      round(buy_mid, 5),
        "buy_battery_low_threshold":  round(buy_battery_low_threshold, 1),
        "buy_battery_high_threshold": round(buy_battery_high_threshold, 1),
    }
