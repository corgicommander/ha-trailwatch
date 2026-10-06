"""Review screen: pick the label for the photo being reviewed."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RevealCoordinator
from .entity import hub_device_info

CHOOSE = "Choose…"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ReviewLabelSelect(hass.data[DOMAIN][entry.entry_id], entry)])


class ReviewLabelSelect(SelectEntity):
    """Choosing a label sorts the photo and moves on to the next one."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Review sort as"
    _attr_translation_key = "review_sort_as"
    _attr_icon = "mdi:tag-arrow-right-outline"

    def __init__(self, coordinator: RevealCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_review_sort_as"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.review.async_add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        return [CHOOSE, *self._coordinator.review.labels]

    @property
    def current_option(self) -> str:
        return CHOOSE

    async def async_select_option(self, option: str) -> None:
        if option == CHOOSE:
            return
        try:
            await self._coordinator.review.async_sort(option)
        except (ValueError, FileNotFoundError) as err:
            raise HomeAssistantError(str(err)) from err
