"""Diagnostics download with credentials and personal details redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLASSIFIER_TOKEN,
    CONF_NOTIFY_DEVICES,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CLASSIFIER_TOKEN,
    CONF_NOTIFY_DEVICES,
    "location",
    "imei",
    "photoUrl",
    "videoUrl",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    cameras = {
        camera_id: {
            "name": data["camera"].get("name"),
            "model": data["camera"].get("model"),
            "status": data["camera"].get("status"),
        }
        for camera_id, data in (coordinator.data or {}).items()
    }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "cameras": cameras,
        "classifier": {
            "local_configured": bool(coordinator.classifier.url),
            "ai_task_entity": coordinator.classifier.ai_entity,
            "ai_task_entity_strong": coordinator.classifier.ai_entity_strong,
            "online": coordinator.health.online,
            "server_version": coordinator.health.version,
            "ai_fallback_error": coordinator.health.ai_error,
        },
        "usage": coordinator.stats.usage,
        "review_counts": coordinator.review_counts,
        "seen_records": len(coordinator._seen),
        "batch": {"running": coordinator.batch_running, "done": coordinator.batch_done, "total": coordinator.batch_total},
    }
