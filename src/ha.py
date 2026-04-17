"""
Home Assistant Supervisor API client.

Inside an add-on, Home Assistant is reachable at http://supervisor/core/api
and auth is a short-lived token injected as $SUPERVISOR_TOKEN.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class HomeAssistant:
    def __init__(self, client: httpx.AsyncClient | None = None):
        token = os.environ["SUPERVISOR_TOKEN"]
        self._owns = client is None
        self._c = client or httpx.AsyncClient(
            base_url="http://supervisor/core/api/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def __aenter__(self) -> "HomeAssistant":
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns:
            await self._c.aclose()

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        r = await self._c.get(f"states/{entity_id}")
        r.raise_for_status()
        return r.json()

    async def get_state_float(self, entity_id: str) -> float:
        s = await self.get_state(entity_id)
        return float(s["state"])

    async def get_attr(self, entity_id: str, attr: str) -> Any:
        s = await self.get_state(entity_id)
        return s.get("attributes", {}).get(attr)

    async def set_input_number(self, entity_id: str, value: float) -> None:
        r = await self._c.post(
            "services/input_number/set_value",
            json={"entity_id": entity_id, "value": round(float(value), 3)},
        )
        r.raise_for_status()
