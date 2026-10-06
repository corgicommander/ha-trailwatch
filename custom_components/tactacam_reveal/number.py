from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_BACKLOG_DAYS, DOMAIN
from .entity import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([BacklogDaysNumber(hass.data[DOMAIN][entry.entry_id], entry)])


class BacklogDaysNumber(RestoreNumber):
    """How many days back the "Process recent photos" button reaches."""

    _attr_has_entity_name = True
    _attr_name = "Days to process"
    _attr_icon = "mdi:calendar-range"
    # Grouped with the processing buttons and classifier status on the device page.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 3650
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.DAYS

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_backlog_days"
        self._attr_device_info = hub_device_info(entry.entry_id)
        self._attr_native_value = DEFAULT_BACKLOG_DAYS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                self._attr_native_value = last.native_value
        self._coordinator.backlog_days = int(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._coordinator.backlog_days = int(value)
        self.async_write_ha_state()
