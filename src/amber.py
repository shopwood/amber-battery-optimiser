"""Trimmed Amber client — only the endpoints this add-on needs."""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Interval:
    channel: str            # 'general' | 'feedIn' | 'controlledLoad'
    nem_time: str
    per_kwh: float          # c/kWh, all-in (predicted for forecasts)
    descriptor: str
    estimate: bool


class Amber:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._owns = client is None
        self._c = client or httpx.AsyncClient(
            base_url="https://api.amber.com.au/v1/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )

    async def __aenter__(self) -> "Amber":
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns:
            await self._c.aclose()

    async def rest_of_day(self, site_id: str, *, next_intervals: int = 48) -> list[Interval]:
        """Current interval plus forecast intervals out to `next_intervals` × 30 min."""
        r = await self._c.get(
            f"sites/{site_id}/prices/current",
            params={"previous": 0, "next": next_intervals, "resolution": 30},
        )
        r.raise_for_status()
        out: list[Interval] = []
        for x in r.json():
            # For forecasts use the P50 predicted value; for current interval use perKwh.
            adv = x.get("advancedPrice") or {}
            per = adv.get("predicted", x["perKwh"])
            out.append(Interval(
                channel=x["channelType"],
                nem_time=x["nemTime"],
                per_kwh=float(per),
                descriptor=x["descriptor"],
                estimate=bool(x.get("estimate", False)),
            ))
        return out
