from __future__ import annotations


from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RevealCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RevealCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        RevealCamera(coordinator, camera_id) for camera_id in coordinator.data
    )


class RevealCamera(CoordinatorEntity[RevealCoordinator], Camera):
    _attr_has_entity_name = True

    def __init__(self, coordinator: RevealCoordinator, camera_id: str) -> None:
        # CoordinatorEntity does not initialize Camera's provider fields in
        # Home Assistant 2026, so both base initializers are required.
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._camera_id = camera_id
        camera = coordinator.data[camera_id]["camera"]
        name = camera.get("name") or f"Reveal {camera_id[-4:]}"
        # The camera is the device's main entity, so use the device name alone.
        self._attr_name = None
        self._attr_unique_id = f"tactacam_reveal_{camera_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, camera_id)},
            "name": name,
            "manufacturer": "Tactacam",
            "model": camera.get("model") or "Reveal",
        }

    @property
    def available(self) -> bool:
        return super().available and self._camera_id in self.coordinator.data

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        photo = self.coordinator.data[self._camera_id].get("photo")
        if not photo or not photo.get("photoUrl"):
            return None
        return await self.coordinator.api.async_image(photo["photoUrl"])

    @property
    def extra_state_attributes(self) -> dict:
        photo = self.coordinator.data[self._camera_id].get("photo") or {}
        camera = self.coordinator.data[self._camera_id]["camera"]
        return {
            "photo_id": photo.get("photoId"),
            "captured_at": photo.get("createdAt") or photo.get("dateCreated"),
            "location": camera.get("location"),
            "battery": (camera.get("status") or {}).get("batteryLevel"),
            "signal": (camera.get("status") or {}).get("signal"),
        }
