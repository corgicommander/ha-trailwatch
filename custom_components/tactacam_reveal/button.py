from __future__ import annotations

from datetime import timedelta

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .archive import FALSE_POSITIVES, REFERENCE
from .const import DOMAIN
from .coordinator import RevealCoordinator
from .entity import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RevealCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ProcessPhotosButton(coordinator, entry, recent=True),
            ProcessPhotosButton(coordinator, entry, recent=False),
            *(ReviewButton(coordinator, entry, action) for action in ReviewButton.ACTIONS),
        ]
    )


class ProcessPhotosButton(ButtonEntity):
    """Classify and sort archived photos that have not been sorted yet."""

    _attr_has_entity_name = True
    # Shown together with "Classifier online" and "Days to process"; the device
    # page sorts each section by name, so "recent" lists before "whole".
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: RevealCoordinator, entry: ConfigEntry, *, recent: bool
    ) -> None:
        self._coordinator = coordinator
        self._recent = recent
        key = "recent" if recent else "backlog"
        self._attr_unique_id = f"{entry.entry_id}_process_{key}"
        self._attr_name = "Process recent photos" if recent else "Process whole backlog"
        self._attr_icon = "mdi:image-search" if recent else "mdi:image-multiple"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_press(self) -> None:
        if not self._coordinator.classifier.enabled:
            raise HomeAssistantError(
                "Set a classifier server or AI Task entity in the integration options"
            )
        since = None
        if self._recent:
            days = self._coordinator.backlog_days
            since = (dt_util.now().date() - timedelta(days=days)).isoformat()
        try:
            await self._coordinator.async_classify_archive(None, since, None, False)
        except RuntimeError as err:
            raise HomeAssistantError(str(err)) from err


class ReviewButton(ButtonEntity):
    """Review screen actions for the photo being reviewed."""

    # action: (name, icon)
    ACTIONS = {
        "previous": ("Review previous", "mdi:chevron-left"),
        "next": ("Review skip", "mdi:chevron-right"),
        "empty": ("Review empty", "mdi:image-off-outline"),
        "reference": ("Review use as reference", "mdi:image-filter-center-focus"),
        "ask_ai": ("Review ask AI", "mdi:robot-outline"),
    }
    _attr_has_entity_name = True

    def __init__(self, coordinator: RevealCoordinator, entry: ConfigEntry, action: str) -> None:
        self._coordinator = coordinator
        self._action = action
        self._attr_name, self._attr_icon = self.ACTIONS[action]
        self._attr_translation_key = f"review_{action}"
        self._attr_unique_id = f"{entry.entry_id}_review_{action}"
        self._attr_device_info = hub_device_info(entry.entry_id)

    async def async_press(self) -> None:
        review = self._coordinator.review
        try:
            if self._action == "previous":
                review.step(-1)
            elif self._action == "next":
                review.step(1)
            elif self._action == "empty":
                await review.async_sort(FALSE_POSITIVES)
            elif self._action == "reference":
                await review.async_sort(REFERENCE)
            else:
                await review.async_ask_ai()
        except (ValueError, FileNotFoundError) as err:
            raise HomeAssistantError(str(err)) from err
        except HomeAssistantError:
            raise
        except Exception as err:  # AI provider errors: show them, don't crash.
            raise HomeAssistantError(f"The AI request failed: {err}") from err
