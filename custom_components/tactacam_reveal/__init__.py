from __future__ import annotations

import hashlib
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import RevealApi
from .archive import (
    REFERENCE,
    category_folder,
    media_id_for,
    move_to_category,
    relative_from_media_id,
)
from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    archive_location,
    locate_media,
)
from .coordinator import RevealCoordinator
from .photo_page import async_register_panel, async_register_websocket

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.TEXT,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SORT_MEDIA = "sort_media"
SERVICE_RELABEL = "relabel"
SERVICE_SET_REFERENCE = "set_reference"
SORT_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Required("media_content_id"): cv.string,
        vol.Required("category"): cv.string,
    }
)
SERVICE_CLASSIFY_MEDIA = "classify_media"
CLASSIFY_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Required("media_content_id"): cv.string,
        vol.Optional("sort", default=True): cv.boolean,
        vol.Optional("fire_event", default=False): cv.boolean,
    }
)

SERVICE_CLASSIFY_ARCHIVE = "classify_archive"
CLASSIFY_ARCHIVE_SCHEMA = vol.Schema(
    {
        vol.Optional("camera"): cv.string,
        vol.Optional("since"): cv.date,
        vol.Optional("until"): cv.date,
        vol.Optional("include_sorted", default=False): cv.boolean,
    }
)


def _coordinator(hass: HomeAssistant) -> RevealCoordinator:
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        raise ServiceValidationError("Tactacam Reveal is not set up")
    coordinator: RevealCoordinator = coordinators[0]
    if not coordinator.classifier.enabled:
        raise ServiceValidationError(
            "Set a classifier server or AI Task entity in the integration options"
        )
    return coordinator


def _relative(hass: HomeAssistant, media_content_id: str) -> str:
    relative = relative_from_media_id(hass, media_content_id)
    if not relative:
        raise ServiceValidationError(
            f"Not a Tactacam Reveal media item: {media_content_id}"
        )
    return relative


CARD_URL = "/tactacam_reveal/tactacam-review-card.js"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the review card and load it on every dashboard (no resource setup)."""
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    path = Path(__file__).parent / "www" / "tactacam-review-card.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(path), cache_headers=False)]
    )
    # A hash of the file (not the version) so browsers and the frontend's
    # service worker never keep running an outdated card after an update.
    digest = await hass.async_add_executor_job(
        lambda: hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={digest}")
    # The same file defines the photo page that phone alerts open.
    await async_register_panel(hass, f"{CARD_URL}?v={digest}")
    async_register_websocket(hass)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await _async_register_card(hass)
    async def async_sort_media(call: ServiceCall) -> dict:
        """Move an archived photo (and its video) into a category subfolder."""
        relative = _relative(hass, call.data["media_content_id"])
        folder = category_folder(call.data["category"])
        _, root = archive_location(hass)
        try:
            new_relative = await hass.async_add_executor_job(
                move_to_category, root, relative, folder
            )
        except FileNotFoundError as err:
            raise ServiceValidationError(f"Media file not found: {relative}") from err
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_refresh_review_counts()
        return {"category": folder, "media_content_id": media_id_for(hass, new_relative)}

    async def async_set_reference(call: ServiceCall) -> dict:
        """Use an archived photo as its camera's empty-scene reference."""
        relative = _relative(hass, call.data["media_content_id"])
        _, root = archive_location(hass)
        try:
            new_relative = await hass.async_add_executor_job(
                move_to_category, root, relative, REFERENCE
            )
        except FileNotFoundError as err:
            raise ServiceValidationError(f"Media file not found: {relative}") from err
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_refresh_review_counts()
        return {"media_content_id": media_id_for(hass, new_relative)}

    async def async_classify_media(call: ServiceCall) -> dict:
        """Classify an archived photo with the configured classifiers."""
        coordinator = _coordinator(hass)
        relative = _relative(hass, call.data["media_content_id"])
        _, root = archive_location(hass)
        found = await hass.async_add_executor_job(locate_media, root, relative)
        if found is None:
            raise ServiceValidationError(f"Media file not found: {relative}")
        relative = found.relative_to(root.resolve()).as_posix()
        return await coordinator.async_classify_and_publish(
            relative,
            {"camera_name": relative.split("/", 1)[0].replace("_", " ")},
            sort=call.data["sort"],
            fire_event=call.data["fire_event"],
        )

    async def async_classify_archive(call: ServiceCall) -> dict:
        """Classify and sort archived photos in the background."""
        coordinator = _coordinator(hass)
        camera = call.data.get("camera")
        try:
            queued = await coordinator.async_classify_archive(
                camera.strip().replace(" ", "_") if camera else None,
                str(call.data["since"]) if "since" in call.data else None,
                str(call.data["until"]) if "until" in call.data else None,
                call.data["include_sorted"],
            )
        except RuntimeError as err:
            raise ServiceValidationError(str(err)) from err
        return {"queued": queued}

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLASSIFY_ARCHIVE,
        async_classify_archive,
        schema=CLASSIFY_ARCHIVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    for name in (SERVICE_SORT_MEDIA, SERVICE_RELABEL):
        hass.services.async_register(
            DOMAIN,
            name,
            async_sort_media,
            schema=SORT_MEDIA_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_REFERENCE,
        async_set_reference,
        schema=vol.Schema({vol.Required("media_content_id"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLASSIFY_MEDIA,
        async_classify_media,
        schema=CLASSIFY_MEDIA_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.title.startswith("Tactacam Reveal ("):
        # Old titles carried a camera count that went stale as cameras were added.
        hass.config_entries.async_update_entry(entry, title="Tactacam Reveal")
    api = RevealApi(
        hass,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = RevealCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await coordinator.async_start_services()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_cancel_tasks()
        return True
    return False


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
