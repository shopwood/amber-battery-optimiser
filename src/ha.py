"""
Home Assistant REST API client.

Used when running as a sidecar docker container next to HA Container.
Base URL points at HA itself (e.g. http://homeassistant:8123), authed with a
long-lived access token (HA → profile → Security → Long-lived access tokens).
"""
from __future__ import annotations

from typing import Any

import httpx


# HA's sentinel state strings for entities whose integrations haven't yet
# pushed a real value. Common at boot before all integrations have warmed up.
_NOT_READY_STATES = {"unknown", "unavailable", "none", ""}


class StateNotReady(RuntimeError):
    """Entity exists but its state hasn't been populated yet.

    Raised by get_state_float when the state is one of HA's sentinel strings
    (unknown/unavailable/none) or otherwise can't be parsed as a float. The
    boot-time retry loop in main treats this the same as a connect failure.
    """


class HomeAssistant:
    def __init__(self, url: str, token: str, client: httpx.AsyncClient | None = None):
        if not token:
            raise RuntimeError("HA_TOKEN is empty — create a long-lived access token in HA and set it in .env")
        self._owns = client is None
        self._c = client or httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/",
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
        state = s.get("state")
        if isinstance(state, str) and state.strip().lower() in _NOT_READY_STATES:
            raise StateNotReady(f"{entity_id} state is {state!r}")
        try:
            return float(state)
        except (TypeError, ValueError) as e:
            raise StateNotReady(f"{entity_id} state {state!r} is not numeric") from e

    async def get_attr(self, entity_id: str, attr: str) -> Any:
        s = await self.get_state(entity_id)
        return s.get("attributes", {}).get(attr)

    async def set_input_number(self, entity_id: str, value: float) -> float:
        """
        Write value to an input_number, clamped to the helper's own min/max and
        step. Returns the value actually written.
        """
        attrs = (await self.get_state(entity_id)).get("attributes", {})
        lo = float(attrs.get("min", value))
        hi = float(attrs.get("max", value))
        step = float(attrs.get("step", 0.001))

        clamped = max(lo, min(hi, float(value)))
        # Round to the helper's step so HA doesn't reject precision mismatches.
        if step > 0:
            clamped = round(clamped / step) * step
        clamped = round(clamped, 3)

        r = await self._c.post(
            "services/input_number/set_value",
            json={"entity_id": entity_id, "value": clamped},
        )
        r.raise_for_status()
        return clamped
