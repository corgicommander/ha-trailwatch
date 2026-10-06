"""Review screen: type a label that is not in the list yet."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RevealCoordinator
from .entity import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ReviewNewLabelText(hass.data[DOMAIN][entry.entry_id], entry)])


class ReviewNewLabelText(TextEntity):
    """Entering e.g. "fox" or "deer, human" sorts the photo under that label."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Review new label"
    _attr_translation_key = "review_new_label"
    _attr_icon = "mdi:tag-plus-outline"
    _attr_native_max = 60
    _attr_native_value = ""

    def __init__(self, coordinator: RevealCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_review_new_label"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_set_value(self, value: str) -> None:
        if not value.strip():
            return
        try:
            await self._coordinator.review.async_sort(value)
        except (ValueError, FileNotFoundError) as err:
            raise HomeAssistantError(str(err)) from err
        # Stay empty, ready for the next one.
        self._attr_native_value = ""
        self.async_write_ha_state()
