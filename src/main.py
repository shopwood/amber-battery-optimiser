"""
Entrypoint. Runs once at startup, then daily at options.run_at local time.

Pipeline:
    1. Load options.
    2. Read SoC + Solcast remaining-today kWh from HA.
    3. Pull rest-of-day Amber forecast.
    4. Compute thresholds.
    5. Write 8 input_numbers (unless dry_run).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from amber import Amber
from config import HELPERS, Options
from ha import HomeAssistant
from optimiser import OptimiserInputs, compute

log = logging.getLogger("optimiser")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def _solcast_remaining_kwh(ha: HomeAssistant, entity: str) -> float:
    """Sum Solcast remaining detailedForecast entries after now."""
    now = datetime.now(ZoneInfo("UTC"))
    detailed = await ha.get_attr(entity, "detailedForecast") or []
    total = 0.0
    for row in detailed:
        # Solcast attribute shape: {"period_start": iso, "pv_estimate": kW, ...}
        start = row.get("period_start")
        if not start:
            continue
        t = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if t < now:
            continue
        # Entries are 30-minute periods; pv_estimate is power (kW) → 0.5 h to get kWh.
        total += float(row.get("pv_estimate", 0.0)) * 0.5
    return total


async def run_once(opts: Options) -> None:
    async with HomeAssistant(opts.ha_url, opts.ha_token) as ha, Amber(opts.amber_token) as amber:
        soc = await ha.get_state_float(opts.soc_entity)
        pv_remaining = await _solcast_remaining_kwh(ha, opts.solcast_forecast_entity)

        intervals = await amber.rest_of_day(opts.amber_site_id, next_intervals=48)
        general = [i.per_kwh for i in intervals if i.channel == "general"]
        feed_in = [i.per_kwh for i in intervals if i.channel == "feedIn"]

        # Naive load estimate: scales with fraction of day remaining. Refine when
        # you wire up your HA energy dashboard.
        now = datetime.now(ZoneInfo(opts.tz))
        fraction_of_day_remaining = max(0.0, (24 - (now.hour + now.minute / 60)) / 24)
        load_remaining = opts.daily_load_kwh * fraction_of_day_remaining

        log.info(
            "inputs: soc=%.1f%%  pv_remaining=%.2fkWh  load_remaining=%.2fkWh  "
            "general_intervals=%d  feed_in_intervals=%d",
            soc, pv_remaining, load_remaining, len(general), len(feed_in),
        )

        values = compute(OptimiserInputs(
            general_prices=general,
            feed_in_prices=feed_in,
            current_soc_pct=soc,
            battery_capacity_kwh=opts.battery_capacity_kwh,
            soc_floor_pct=opts.battery_soc_floor_pct,
            soc_ceiling_pct=opts.battery_soc_ceiling_pct,
            pv_remaining_kwh=pv_remaining,
            load_remaining_kwh=load_remaining,
            sell_high_pct=opts.sell_high_pct,
            sell_low_pct=opts.sell_low_pct,
            buy_low_pct=opts.buy_low_pct,
            buy_mid_pct=opts.buy_mid_pct,
        ))

        log.info("computed: %s", values)

        if opts.dry_run:
            log.info("dry_run=true, not writing helpers")
            return

        for key, value in values.items():
            entity = HELPERS[key]
            try:
                written = await ha.set_input_number(entity, value)
                if written != round(float(value), 3):
                    log.warning("wrote %s = %s (clamped from %s)", entity, written, value)
                else:
                    log.info("wrote %s = %s", entity, written)
            except Exception as e:
                log.warning("failed to write %s = %s: %s", entity, value, e)


async def main() -> None:
    opts = Options.load()
    log.info("starting; run_at=%s dry_run=%s site=%s", opts.run_at, opts.dry_run, opts.amber_site_id)

    # Run once on boot so the helpers aren't stale after a restart.
    try:
        await run_once(opts)
    except Exception:
        log.exception("initial run failed")

    # RUN_AT accepts one or more HH:MM times, comma-separated. E.g. "05:00" or "05:00,17:00".
    times: list[tuple[int, int]] = []
    for item in opts.run_at.split(","):
        item = item.strip()
        if not item:
            continue
        h, m = (int(x) for x in item.split(":"))
        times.append((h, m))

    sched = AsyncIOScheduler(timezone=ZoneInfo(opts.tz))
    for h, m in times:
        sched.add_job(
            lambda: asyncio.create_task(run_once(opts)),
            CronTrigger(hour=h, minute=m),
            name=f"optimise_{h:02d}{m:02d}",
            misfire_grace_time=300,
        )
        log.info("scheduled optimise at %02d:%02d %s", h, m, opts.tz)
    sched.start()

    # Park forever.
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
