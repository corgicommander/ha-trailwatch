from __future__ import annotations

import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .archive import pretty_category
from .const import CONF_AI_COST_PER_CALL, DEFAULT_AI_COST_PER_CALL, DOMAIN
from .coordinator import RevealCoordinator, _safe_name
from .entity import camera_device_info, hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RevealCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for camera_id in coordinator.data:
        entities.extend(
            (
                RevealBatterySensor(coordinator, camera_id),
                RevealSignalSensor(coordinator, camera_id),
            )
        )
        folder = _safe_name(
            coordinator.data[camera_id]["camera"].get("name") or camera_id
        )
        entities.extend(
            (
                LastSightingSensor(coordinator, camera_id, folder),
                SightingsTodaySensor(coordinator, camera_id, folder),
            )
        )
    entities.extend(
        (
            ProcessingSensor(coordinator, entry),
            ReviewQueueSensor(coordinator, entry),
            ReviewDetailsSensor(coordinator, entry),
            VideoRequestsSensor(coordinator, entry),
            *(UsageSensor(coordinator, entry, kind) for kind in UsageSensor.KINDS),
        )
    )
    async_add_entities(entities)


class _StatsEntity(SensorEntity):
    """Sensor fed by the persisted stats or coordinator progress listeners."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: RevealCoordinator) -> None:
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.stats.async_add_listener(self._updated)
        )
        self.async_on_remove(
            self._coordinator.async_add_progress_listener(self._updated)
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()


class LastSightingSensor(_StatsEntity):
    """What a camera saw most recently (empty frames excluded)."""

    _attr_name = "Last sighting"
    _attr_icon = "mdi:paw"

    def __init__(self, coordinator, camera_id: str, folder: str) -> None:
        super().__init__(coordinator)
        self._folder = folder
        self._attr_unique_id = f"tactacam_reveal_{camera_id}_last_sighting"
        self._attr_device_info = camera_device_info(camera_id)

    @property
    def _last(self) -> dict | None:
        return self._coordinator.stats.camera(self._folder).get("last")

    @property
    def native_value(self) -> str | None:
        last = self._last
        return pretty_category(last["category"]) if last else None

    @property
    def extra_state_attributes(self) -> dict:
        last = self._last or {}
        return {
            "subject": last.get("subject"),
            "reason": last.get("reason"),
            "decided_by": last.get("source"),
            "seen_at": last.get("time"),
            "labels": last.get("labels"),
        }


class SightingsTodaySensor(_StatsEntity):
    """Sightings so far today, with a count per animal."""

    _attr_name = "Sightings today"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "sightings"

    def __init__(self, coordinator, camera_id: str, folder: str) -> None:
        super().__init__(coordinator)
        self._folder = folder
        self._attr_unique_id = f"tactacam_reveal_{camera_id}_sightings_today"
        self._attr_device_info = camera_device_info(camera_id)

    @property
    def native_value(self) -> int:
        return sum(self._coordinator.stats.camera(self._folder)["today"]["counts"].values())

    @property
    def extra_state_attributes(self) -> dict:
        return dict(self._coordinator.stats.camera(self._folder)["today"]["counts"])


class ReviewQueueSensor(_StatsEntity):
    """Photos the classifiers were unsure about, waiting for a person."""

    _attr_name = "Photos to review"
    _attr_translation_key = "photos_to_review"
    _attr_icon = "mdi:image-search-outline"
    _attr_native_unit_of_measurement = "photos"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_review_queue"
        self._attr_device_info = hub_device_info(entry.entry_id)

    @property
    def native_value(self) -> int:
        return sum(self._coordinator.review_counts.values())

    @property
    def extra_state_attributes(self) -> dict:
        return {
            camera.replace("_", " "): count
            for camera, count in self._coordinator.review_counts.items()
        }


class UsageSensor(_StatsEntity):
    """Local versus cloud classification this month, and what that saved."""

    KINDS = ("local", "cloud_calls", "local_share", "savings", "cloud_cost")
    _NAMES = {
        "local": ("Local classifications this month", "mdi:desktop-classic"),
        "cloud_calls": ("Cloud AI calls this month", "mdi:cloud-outline"),
        "local_share": ("Local share this month", "mdi:chart-donut"),
        "savings": ("Estimated AI savings this month", "mdi:piggy-bank-outline"),
        "cloud_cost": ("Estimated AI cost this month", "mdi:cash"),
    }

    def __init__(self, coordinator, entry: ConfigEntry, kind: str) -> None:
        super().__init__(coordinator)
        self._kind = kind
        self._attr_name, self._attr_icon = self._NAMES[kind]
        self._attr_unique_id = f"{entry.entry_id}_usage_{kind}"
        self._attr_device_info = hub_device_info(entry.entry_id)
        self._cost = float(
            coordinator.options.get(CONF_AI_COST_PER_CALL, DEFAULT_AI_COST_PER_CALL)
        )
        if kind in ("savings", "cloud_cost"):
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = "USD"
            self._attr_suggested_display_precision = 2
        elif kind == "local_share":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_suggested_display_precision = 0
        else:
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_native_unit_of_measurement = "photos" if kind == "local" else "calls"

    @property
    def native_value(self):
        usage = self._coordinator.stats.usage
        local, ai = usage["local_month"], usage["ai_month"]
        if self._kind == "local":
            return local
        if self._kind == "cloud_calls":
            return usage["ai_calls_month"]
        if self._kind == "local_share":
            return round(100 * local / (local + ai), 1) if local + ai else None
        if self._kind == "savings":
            # Every photo settled locally is a cloud call that was not needed.
            return round(local * self._cost, 2)
        return round(usage["ai_calls_month"] * self._cost, 2)

    @property
    def extra_state_attributes(self) -> dict:
        usage = self._coordinator.stats.usage
        return {
            "today_local": usage["local_day"],
            "today_cloud_photos": usage["ai_day"],
            "today_cloud_calls": usage["ai_calls_day"],
            "month_cloud_photos": usage["ai_month"],
            "month_unsorted": usage["unsorted_month"],
            "cost_per_cloud_call": self._cost,
            "month": usage.get("month"),
        }


class ProcessingSensor(SensorEntity):
    """Progress of a running archive classification, or idle."""

    _attr_has_entity_name = True
    _attr_name = "Photo processing"
    _attr_icon = "mdi:progress-clock"
    _attr_should_poll = False

    def __init__(self, coordinator: RevealCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_processing"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_progress_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> str:
        c = self._coordinator
        if not c.batch_running:
            return "idle"
        return f"{c.batch_done}/{c.batch_total}"

    @property
    def extra_state_attributes(self) -> dict:
        c = self._coordinator
        return {"processed": c.batch_done, "total": c.batch_total}


class RevealStatusSensor(CoordinatorEntity[RevealCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: RevealCoordinator, camera_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._camera_id = camera_id
        self._key = key
        camera = coordinator.data[camera_id]["camera"]
        camera_name = camera.get("name") or f"Reveal {camera_id[-4:]}"
        self._attr_unique_id = f"tactacam_reveal_{camera_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, camera_id)},
            "name": camera_name,
            "manufacturer": "Tactacam",
            "model": camera.get("model") or "Reveal",
        }

    @property
    def native_value(self):
        camera = self.coordinator.data.get(self._camera_id, {}).get("camera", {})
        return (camera.get("status") or {}).get(self._key)


class RevealBatterySensor(RevealStatusSensor):
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery"

    def __init__(self, coordinator: RevealCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, camera_id, "batteryLevel")


class RevealSignalSensor(RevealStatusSensor):
    _attr_name = "Signal"
    _attr_native_unit_of_measurement = "level"
    _attr_icon = "mdi:signal-cellular-3"

    def __init__(self, coordinator: RevealCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, camera_id, "signal")


class ReviewDetailsSensor(SensorEntity):
    """Position in the review queue, with what the classifier thought."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Review details"
    # Translation keys let the review card find its entities.
    _attr_translation_key = "review_details"
    _attr_icon = "mdi:image-search"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_review_details"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.review.async_add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        review = self._coordinator.review
        if not review.items:
            return "Nothing to review"
        return f"{review.index + 1} of {len(review.items)}"

    @property
    def extra_state_attributes(self) -> dict:
        review = self._coordinator.review
        relative = review.current
        note = review.note(relative)
        name = relative.rsplit("/", 1)[-1] if relative else None
        return {
            "camera": relative.split("/", 1)[0].replace("_", " ") if relative else None,
            "file": name,
            "captured": _captured(name),
            "has_video": bool(relative) and _video_exists(self._coordinator, relative),
            "guess": note.get("subject"),
            "reason": note.get("reason"),
            "decided_by": note.get("source"),
            "asking_ai": review.busy,
            "media_content_id": self._coordinator.media_id(relative) if relative else None,
            "labels": review.labels,
        }


def _captured(name: str | None) -> str | None:
    match = re.search(r"-(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})-", name or "")
    if not match:
        return None
    month, day, year, hour, minute, _ = match.groups()
    return f"{year}-{month}-{day} {hour}:{minute}"


def _video_exists(coordinator, relative: str) -> bool:
    # Uses the review queue's cached listing; no file access on the event loop.
    return relative.removesuffix("_photo.jpg") + "_video.mp4" in coordinator.review.videos


class VideoRequestsSensor(SensorEntity):
    """Automatic video requests: queued, outstanding and the last outcome."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Video requests"
    _attr_translation_key = "video_requests"
    _attr_icon = "mdi:video-wireless-outline"
    _attr_native_unit_of_measurement = "videos"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_video_requests"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.videos.async_add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        videos = self._coordinator.videos
        return len(videos.queue) + (1 if videos.pending else 0)

    @property
    def extra_state_attributes(self) -> dict:
        videos = self._coordinator.videos
        pending = videos.pending or {}
        return {
            "automatic_requests": videos.auto_request,
            "waiting_for": pending.get("relative", "").rsplit("/", 1)[-1] or None,
            "requested_at": pending.get("requested_iso"),
            "queued": [q["relative"].rsplit("/", 1)[-1] for q in videos.queue],
            "last_result": videos.last_result,
        }
