"""Connectivity of the local camera-trap classifier server."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RevealCoordinator
from .entity import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RevealCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ClassifierOnlineSensor(coordinator, entry)])


class ClassifierOnlineSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Classifier online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: RevealCoordinator, entry: ConfigEntry) -> None:
        self._health = coordinator.health
        self._attr_unique_id = f"{entry.entry_id}_classifier_online"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._health.async_add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Without a configured server there is nothing to report.
        return self._health.enabled

    @property
    def is_on(self) -> bool | None:
        return self._health.online

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "url": self._health.url,
            "server_version": self._health.version,
            "ai_fallback_error": self._health.ai_error,
        }
