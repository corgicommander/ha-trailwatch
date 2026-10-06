"""Watch the local classifier and the AI fallback; surface problems in Repairs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import CLASSIFIER_HEALTH_INTERVAL, CLASSIFIER_OFFLINE_ISSUE_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_CLASSIFIER_OFFLINE = "classifier_offline"
ISSUE_AI_FAILING = "ai_fallback_failing"


class ClassifierHealth:
    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self.hass = hass
        self.url = url
        self.token = token
        self.online: bool | None = None
        self.version: str | None = None
        self.offline_since = None
        self.ai_error: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsub = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        if self.enabled:
            await self.async_check()
            self._unsub = async_track_time_interval(
                self.hass, self.async_check, timedelta(seconds=CLASSIFIER_HEALTH_INTERVAL)
            )

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def async_check(self, _now=None) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with async_get_clientsession(self.hass).get(
                f"{self.url}/health", headers=headers, timeout=ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                body = await response.json()
            online = body.get("status") == "ok"
            self.version = body.get("version")
        except (ClientError, TimeoutError, OSError, ValueError):
            online = False
        now = dt_util.utcnow()
        if online:
            self.offline_since = None
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CLASSIFIER_OFFLINE)
        else:
            self.offline_since = self.offline_since or now
            if now - self.offline_since >= timedelta(minutes=CLASSIFIER_OFFLINE_ISSUE_MINUTES):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_CLASSIFIER_OFFLINE,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_CLASSIFIER_OFFLINE,
                    translation_placeholders={"url": self.url},
                )
        if online != self.online:
            self.online = online
            self._changed()

    @callback
    def async_ai_result(self, ok: bool, error: str | None) -> None:
        """Track the AI fallback; raise a Repairs issue while it is failing."""
        if ok:
            if self.ai_error is not None:
                self.ai_error = None
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_AI_FAILING)
                self._changed()
            return
        self.ai_error = (error or "unknown error")[:300]
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_AI_FAILING,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_AI_FAILING,
            translation_placeholders={"error": self.ai_error},
        )
        self._changed()
