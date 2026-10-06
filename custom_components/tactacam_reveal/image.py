"""Per-camera image of the most recent real sighting."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, archive_location, locate_media
from .coordinator import RevealCoordinator, _safe_name
from .entity import camera_device_info, hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RevealCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ImageEntity] = [
        LastSightingImage(
            hass,
            coordinator,
            camera_id,
            _safe_name(coordinator.data[camera_id]["camera"].get("name") or camera_id),
        )
        for camera_id in coordinator.data
    ]
    entities.append(ReviewImage(hass, coordinator, entry))
    async_add_entities(entities)


class LastSightingImage(ImageEntity):
    """The archived photo of a camera's last sighting (not its last trigger)."""

    _attr_has_entity_name = True
    _attr_name = "Last sighting"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass, coordinator: RevealCoordinator, camera_id: str, folder: str) -> None:
        super().__init__(hass)
        self._coordinator = coordinator
        self._folder = folder
        self._attr_unique_id = f"tactacam_reveal_{camera_id}_last_sighting_image"
        self._attr_device_info = camera_device_info(camera_id)
        self._relative: str | None = None
        self._sync()

    def _sync(self) -> bool:
        last = self._coordinator.stats.camera(self._folder).get("last") or {}
        if last.get("relative") == self._relative:
            return False
        self._relative = last.get("relative")
        seen = dt_util.parse_datetime(last["time"]) if last.get("time") else None
        self._attr_image_last_updated = seen or datetime.min.replace(tzinfo=dt_util.UTC)
        return True

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.stats.async_add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        if self._sync():
            self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        if not self._relative:
            return None
        _, root = archive_location(self.hass)
        path = await self.hass.async_add_executor_job(locate_media, root, self._relative)
        if path is None:
            return None
        return await self.hass.async_add_executor_job(path.read_bytes)


class ReviewImage(ImageEntity):
    """The photo currently shown on the review screen."""

    _attr_has_entity_name = True
    _attr_name = "Review photo"
    _attr_translation_key = "review_photo"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass, coordinator: RevealCoordinator, entry: ConfigEntry) -> None:
        super().__init__(hass)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_review_photo"
        self._attr_device_info = hub_device_info(entry.entry_id)
        self._relative: str | None = None
        self._sync()

    def _sync(self) -> bool:
        current = self._coordinator.review.current
        if current == self._relative and self._attr_image_last_updated is not None:
            return False
        self._relative = current
        # A new timestamp makes the frontend fetch the new picture.
        self._attr_image_last_updated = dt_util.utcnow()
        return True

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.review.async_add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        if self._sync():
            self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        if not self._relative:
            return None
        _, root = archive_location(self.hass)
        path = await self.hass.async_add_executor_job(locate_media, root, self._relative)
        if path is None:
            return None
        return await self.hass.async_add_executor_job(path.read_bytes)
