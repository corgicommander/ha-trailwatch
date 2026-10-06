"""Persisted sighting and classifier-usage statistics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .archive import FALSE_POSITIVES, UNCERTAIN
from .const import DOMAIN

USAGE_KEYS = ("local", "ai", "ai_calls", "unsorted")


def _empty_usage() -> dict[str, int]:
    return {f"{key}_{period}": 0 for key in USAGE_KEYS for period in ("day", "month")}


class TactacamStats:
    """Last sightings and daily counts per camera, plus local/cloud usage."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.stats")
        self.data: dict[str, Any] = {"cameras": {}, "usage": _empty_usage()}
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.data = stored
            self.data.setdefault("cameras", {})
            self.data["usage"] = {**_empty_usage(), **self.data.get("usage", {})}
        self._roll()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _roll(self) -> None:
        """Reset daily and monthly counters when the date changes."""
        today = dt_util.now().date()
        usage = self.data["usage"]
        if usage.get("day") != today.isoformat():
            for key in USAGE_KEYS:
                usage[f"{key}_day"] = 0
            usage["day"] = today.isoformat()
        if usage.get("month") != today.strftime("%Y-%m"):
            for key in USAGE_KEYS:
                usage[f"{key}_month"] = 0
            usage["month"] = today.strftime("%Y-%m")
        for camera in self.data["cameras"].values():
            if camera.get("today", {}).get("date") != today.isoformat():
                camera["today"] = {"date": today.isoformat(), "counts": {}}

    def camera(self, folder: str) -> dict[str, Any]:
        self._roll()
        return self.data["cameras"].setdefault(
            folder,
            {"last": None, "today": {"date": dt_util.now().date().isoformat(), "counts": {}}},
        )

    @property
    def usage(self) -> dict[str, Any]:
        self._roll()
        return self.data["usage"]

    @callback
    def async_record(self, folder: str, result: dict[str, Any], *, live: bool) -> None:
        """Count one classification; live photos also update sightings."""
        self._roll()
        usage = self.data["usage"]
        source = result.get("source") if result.get("source") in ("local", "ai") else "unsorted"
        for period in ("day", "month"):
            usage[f"{source}_{period}"] += 1
            usage[f"ai_calls_{period}"] += int(result.get("ai_calls", 0))
        category = result.get("category")
        if live and category not in (FALSE_POSITIVES, UNCERTAIN, None):
            camera = self.camera(folder)
            camera["last"] = {
                "category": category,
                "labels": result.get("labels", []),
                "subject": result.get("subject", ""),
                "reason": result.get("reason", ""),
                "source": result.get("source"),
                "captured_at": result.get("captured_at"),
                "time": dt_util.now().isoformat(),
                "relative": result.get("relative"),
            }
            counts = camera["today"]["counts"]
            for label in result.get("labels") or [category]:
                counts[label] = counts.get(label, 0) + 1
        self._store.async_delay_save(lambda: self.data, 10)
        for listener in list(self._listeners):
            listener()
