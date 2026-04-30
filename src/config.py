"""
Config loader (env-driven — for HA Container, running as a sidecar docker service).
Reads from environment variables; set via the .env file referenced in compose.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default=None, cast=lambda x: x):
    v = os.environ.get(key)
    return cast(v) if v not in (None, "") else default


@dataclass(frozen=True)
class Options:
    # HA connection
    ha_url: str
    ha_token: str
    # Amber
    amber_token: str
    amber_site_id: str
    # Schedule
    dry_run: bool
    # Entities
    soc_entity: str
    general_price_entity: str
    feed_in_price_entity: str
    solcast_forecast_entity: str
    solcast_forecast_tomorrow_entity: str
    # Physical battery
    battery_capacity_kwh: float
    battery_soc_floor_pct: float
    battery_soc_ceiling_pct: float
    battery_charge_rate_kw: float   # max charge rate used for buy-quantity estimates
    # Load estimate
    daily_load_kwh: float
    # Sell percentile bands
    sell_high_pct: int
    sell_low_pct: int
    # Emergency buy percentile (when battery is critically low)
    buy_low_pct: int
    # Mid buy: price-scan strategy targeting buy_target_soc_pct
    buy_target_soc_pct: float       # aim to reach this SoC via grid charging (e.g. 85%)
    buy_max_price_cents: float      # never pay more than this (c/kWh) for mid-band buying
    buy_forecast_adjustment_cents: float  # add to forecast general prices to compensate for P50 under-bias
    # Hard guardrails
    min_sell_soc_pct: float
    max_buy_soc_pct: float
    sell_price_floor: float         # $/kWh
    # Schedule — run hourly from run_hourly_from to run_hourly_to (inclusive, local time)
    run_hourly_from: int
    run_hourly_to: int
    # Timezone
    tz: str

    @classmethod
    def load(cls) -> "Options":
        return cls(
            ha_url=_env("HA_URL", "http://homeassistant:8123"),
            ha_token=_env("HA_TOKEN", ""),
            amber_token=_env("AMBER_TOKEN", ""),
            amber_site_id=_env("AMBER_SITE_ID", ""),
            dry_run=_env("DRY_RUN", False, lambda s: str(s).lower() in ("1", "true", "yes")),
            soc_entity=_env("SOC_ENTITY", "sensor.esy_sunhome_1926470123495710721_battery_state_of_charge"),
            general_price_entity=_env("GENERAL_PRICE_ENTITY", "sensor.01k1z85jvtmqnfb3h5cs6yd95y_general_price"),
            feed_in_price_entity=_env("FEED_IN_PRICE_ENTITY", "sensor.01k1z85jvtmqnfb3h5cs6yd95y_feed_in_price"),
            solcast_forecast_entity=_env("SOLCAST_FORECAST_ENTITY", "sensor.solcast_pv_forecast_forecast_today"),
            solcast_forecast_tomorrow_entity=_env("SOLCAST_FORECAST_TOMORROW_ENTITY", "sensor.solcast_pv_forecast_forecast_tomorrow"),
            battery_capacity_kwh=_env("BATTERY_CAPACITY_KWH", 10.0, float),
            battery_soc_floor_pct=_env("BATTERY_SOC_FLOOR_PCT", 10.0, float),
            battery_soc_ceiling_pct=_env("BATTERY_SOC_CEILING_PCT", 100.0, float),
            battery_charge_rate_kw=_env("BATTERY_CHARGE_RATE_KW", 5.0, float),
            daily_load_kwh=_env("DAILY_LOAD_KWH", 20.0, float),
            sell_high_pct=_env("SELL_HIGH_PCT", 90, int),
            sell_low_pct=_env("SELL_LOW_PCT", 70, int),
            buy_low_pct=_env("BUY_LOW_PCT", 10, int),
            buy_target_soc_pct=_env("BUY_TARGET_SOC_PCT", 85.0, float),
            buy_max_price_cents=_env("BUY_MAX_PRICE_CENTS", 12.0, float),
            buy_forecast_adjustment_cents=_env("BUY_FORECAST_ADJUSTMENT_CENTS", 1.0, float),
            min_sell_soc_pct=_env("MIN_SELL_SOC_PCT", 20.0, float),
            max_buy_soc_pct=_env("MAX_BUY_SOC_PCT", 90.0, float),
            sell_price_floor=_env("SELL_PRICE_FLOOR", 0.15, float),
            run_hourly_from=_env("RUN_HOURLY_FROM", 7, int),
            run_hourly_to=_env("RUN_HOURLY_TO", 14, int),
            tz=_env("TZ", "Australia/Sydney"),
        )


# Input_number helper IDs written by the optimiser.
HELPERS = {
    "sell_price_threshold":       "input_number.sell_price_threshold",
    "sell_battery_minimum":       "input_number.sell_battery_minimum",
    "sell_price_low_threshold":   "input_number.sell_price_low_threshold",
    "sell_low_battery_minimum":   "input_number.sell_low_battery_minimum",
    "buy_price_low_battery":      "input_number.buy_price_low_battery",
    "buy_battery_low_threshold":  "input_number.buy_battery_low_threshold",
    "buy_price_mid_battery":      "input_number.buy_price_mid_battery",
    "buy_battery_high_threshold": "input_number.buy_battery_high_threshold",
}
