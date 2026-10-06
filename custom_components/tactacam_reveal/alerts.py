"""Decide which classified photos deserve a notification, and send them."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .archive import pretty_category
from .photo_page import photo_page_path
from .const import (
    CONF_CAMERA_NOTIFY,
    CONF_CAMERAS,
    CONF_NOTIFY_COOLDOWN,
    CONF_NOTIFY_DEVICES,
    CONF_ONLY_NOTIFY_LABELS,
    CONF_QUIET_LABELS,
    DEFAULT_NOTIFY_COOLDOWN,
    DEFAULT_QUIET_LABELS,
)

_LOGGER = logging.getLogger(__name__)


def parse_labels(value: str | None) -> set[str]:
    return {item.strip().lower() for item in (value or "").split(",") if item.strip()}


class AlertPolicy:
    """Per-camera notification rules plus a cooldown for photo bursts."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        self.hass = hass
        self.devices: list[str] = list(options.get(CONF_NOTIFY_DEVICES) or [])
        self.cooldown = timedelta(
            minutes=int(options.get(CONF_NOTIFY_COOLDOWN, DEFAULT_NOTIFY_COOLDOWN))
        )
        self.quiet = parse_labels(options.get(CONF_QUIET_LABELS, DEFAULT_QUIET_LABELS))
        self.only = parse_labels(options.get(CONF_ONLY_NOTIFY_LABELS))
        self.cameras: dict[str, dict] = options.get(CONF_CAMERAS) or {}
        self._last_sent: dict[tuple[str, str], datetime] = {}

    def rules_for(self, folder: str) -> tuple[bool, set[str], set[str]]:
        """(notifications on, only-notify labels, never-notify labels)."""
        camera = self.cameras.get(folder) or {}
        only = camera.get(CONF_ONLY_NOTIFY_LABELS)
        quiet = camera.get(CONF_QUIET_LABELS)
        return (
            camera.get(CONF_CAMERA_NOTIFY, True),
            parse_labels(only) if only else self.only,
            parse_labels(quiet) if quiet else self.quiet,
        )

    def decide(self, folder: str, result: dict[str, Any]) -> tuple[bool, str | None]:
        """Return (notify, why not). Records the alert for the cooldown."""
        if not result.get("notify"):
            return False, "not a sighting"
        enabled, only, quiet = self.rules_for(folder)
        if not enabled:
            return False, "notifications are off for this camera"
        labels = set(result.get("labels") or [result.get("category")])
        wanted = labels - quiet
        if not wanted:
            return False, "only quiet labels"
        if only:
            wanted &= only
            if not wanted:
                return False, "not on the notify list"
        now = dt_util.utcnow()
        fresh = [
            label
            for label in wanted
            if now - self._last_sent.get((folder, label), datetime.min.replace(tzinfo=now.tzinfo))
            >= self.cooldown
        ]
        if not fresh:
            return False, "cooldown"
        for label in wanted:
            self._last_sent[(folder, label)] = now
        return True, None

    async def async_send(self, data: dict[str, Any]) -> None:
        """Push a photo notification to every configured phone."""
        if not self.devices:
            return
        from homeassistant.components.mobile_app.util import (
            get_notify_service,
            webhook_id_from_device_id,
        )

        title = f"{data.get('camera_name', 'Trail camera')}: {pretty_category(data.get('category', ''))}"
        image_url = data.get("image_url")
        # Tapping opens the photo page (save, share, request video, close).
        # Plain query parameters: media-browser deep links do not survive the
        # iOS app's URL handling.
        page = photo_page_path(data.get("relative") or "")
        payload = {
            "title": title,
            "message": data.get("reason") or data.get("subject") or "New sighting",
            "data": {
                "image": image_url,
                "url": page,
                "clickAction": page,
                "group": "tactacam_reveal",
            },
        }
        for device_id in self.devices:
            webhook_id = webhook_id_from_device_id(self.hass, device_id)
            service = webhook_id and get_notify_service(self.hass, webhook_id)
            if not service:
                _LOGGER.warning("No notify service for mobile device %s", device_id)
                continue
            try:
                await self.hass.services.async_call("notify", service, payload, blocking=True)
            except Exception:
                _LOGGER.exception("Unable to send Tactacam notification via %s", service)

