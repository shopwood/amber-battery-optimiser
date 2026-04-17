"""Add-on options loader. HA writes them to /data/options.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")


@dataclass(frozen=True)
class Options:
    amber_token: str
    amber_site_id: str
    run_at: str
    dry_run: bool
    soc_entity: str
    general_price_entity: str
    feed_in_price_entity: str
    solcast_forecast_entity: str
    battery_capacity_kwh: float
    battery_soc_floor_pct: float
    battery_soc_ceiling_pct: float
    daily_load_kwh: float
    sell_high_pct: int
    sell_low_pct: int
    buy_low_pct: int
    buy_mid_pct: int

    @classmethod
    def load(cls) -> "Options":
        raw = json.loads(OPTIONS_PATH.read_text()) if OPTIONS_PATH.exists() else {}
        # Allow env overrides for local dev outside HA.
        def g(k, default=None, cast=lambda x: x):
            v = os.environ.get(k.upper(), raw.get(k, default))
            return cast(v) if v is not None else default
        return cls(
            amber_token=g("amber_token", ""),
            amber_site_id=g("amber_site_id", ""),
            run_at=g("run_at", "05:00"),
            dry_run=bool(g("dry_run", False)),
            soc_entity=g("soc_entity", ""),
            general_price_entity=g("general_price_entity", ""),
            feed_in_price_entity=g("feed_in_price_entity", ""),
            solcast_forecast_entity=g("solcast_forecast_entity", ""),
            battery_capacity_kwh=float(g("battery_capacity_kwh", 10.0)),
            battery_soc_floor_pct=float(g("battery_soc_floor_pct", 10.0)),
            battery_soc_ceiling_pct=float(g("battery_soc_ceiling_pct", 100.0)),
            daily_load_kwh=float(g("daily_load_kwh", 20.0)),
            sell_high_pct=int(g("sell_high_pct", 90)),
            sell_low_pct=int(g("sell_low_pct", 70)),
            buy_low_pct=int(g("buy_low_pct", 10)),
            buy_mid_pct=int(g("buy_mid_pct", 30)),
        )


# Input_number helper IDs written by the optimiser. Hard-coded to match the
# user's existing HA automation; lives here so nothing else has to know the names.
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
