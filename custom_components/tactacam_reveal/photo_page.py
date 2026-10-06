"""The photo page that phone alerts open: /tactacam-photo?camera=<folder>&file=<name>.

Plain query parameters (no percent-encoding) survive the iOS app's link
handling, unlike media-browser deep links. The page itself is the
`tactacam-photo-panel` element in www/tactacam-review-card.js.
"""

from __future__ import annotations

from datetime import timedelta
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback

from .archive import capture_date, media_url_path, pretty_category
from .const import DOMAIN, archive_location, locate_media

PANEL_PATH = "tactacam-photo"
SAFE_NAME = re.compile(r"[\w.-]+")


def photo_page_path(relative: str) -> str:
    """Frontend path of the photo page for an archived photo."""
    camera, _, rest = relative.partition("/")
    return f"/{PANEL_PATH}?camera={camera}&file={rest.rsplit('/', 1)[-1]}"


def _photo_id(name: str) -> str | None:
    """Reveal photo id from '<date>_<time>_<photo id>_photo.jpg'."""
    parts = name.removesuffix("_photo.jpg").split("_", 2)
    return parts[2] if len(parts) == 3 else None


def _coordinator(hass: HomeAssistant):
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    return coordinators[0] if coordinators else None


async def _async_locate(hass: HomeAssistant, camera: str, file: str) -> str | None:
    if not (SAFE_NAME.fullmatch(camera) and SAFE_NAME.fullmatch(file)):
        return None
    _, root = archive_location(hass)
    path = await hass.async_add_executor_job(locate_media, root, f"{camera}/{file}")
    return path.relative_to(root.resolve()).as_posix() if path else None


def _video_state(coordinator, relative: str, name: str) -> str:
    """none | available | requested | waiting | downloaded."""
    if "-V-" not in name:
        return "none"
    videos = coordinator.videos if coordinator else None
    photo_id = _photo_id(name)
    if videos and videos.pending and videos.pending.get("photo_id") == photo_id:
        return "requested"
    if videos and any(q.get("photo_id") == photo_id for q in videos.queue):
        return "waiting"
    return "available"


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/photo",
        vol.Required("camera"): str,
        vol.Required("file"): str,
    }
)
@websocket_api.async_response
async def ws_photo(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Where a photo is now (it may have been sorted), with signed links."""
    relative = await _async_locate(hass, msg["camera"], msg["file"])
    if relative is None:
        connection.send_error(msg["id"], "not_found", "This photo is no longer in the archive")
        return
    _, root = archive_location(hass)
    name = relative.rsplit("/", 1)[-1]
    video_relative = relative.removesuffix("_photo.jpg") + "_video.mp4"
    has_video = await hass.async_add_executor_job((root / video_relative).is_file)

    def sign(path: str) -> str:
        return async_sign_path(
            hass,
            media_url_path(hass, path),
            timedelta(hours=12),
            refresh_token_id=connection.refresh_token_id,
        )

    parts = relative.split("/")
    folder = parts[1] if len(parts) > 2 else None
    coordinator = _coordinator(hass)
    connection.send_result(
        msg["id"],
        {
            "camera": parts[0].replace("_", " "),
            "category": pretty_category(folder) if folder else "Unsorted",
            "captured": _captured(name),
            "date": capture_date(name),
            "file": name,
            "photo_url": sign(relative),
            "video_url": sign(video_relative) if has_video else None,
            "video": "downloaded" if has_video else _video_state(coordinator, relative, name),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/request_video",
        vol.Required("camera"): str,
        vol.Required("file"): str,
    }
)
@websocket_api.async_response
async def ws_request_video(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Ask Reveal for the video behind a photo (queued: one at a time)."""
    coordinator = _coordinator(hass)
    relative = await _async_locate(hass, msg["camera"], msg["file"])
    name = relative.rsplit("/", 1)[-1] if relative else ""
    photo_id = _photo_id(name)
    if coordinator is None or relative is None or not photo_id or "-V-" not in name:
        connection.send_error(msg["id"], "not_available", "This photo has no video to request")
        return
    camera_id = photo_id.split("-", 1)[0]
    await coordinator.videos.async_consider(
        {"photo_id": photo_id, "camera_id": camera_id, "relative": relative, "labels": []}
    )
    connection.send_result(msg["id"], {"video": _video_state(coordinator, relative, name)})


def _captured(name: str) -> str | None:
    match = re.search(r"-(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})-", name)
    if not match:
        return None
    month, day, year, hour, minute, _ = match.groups()
    return f"{year}-{month}-{day} {hour}:{minute}"


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_photo)
    websocket_api.async_register_command(hass, ws_request_video)


async def async_register_panel(hass: HomeAssistant, module_url: str) -> None:
    """Register the page without a sidebar entry."""
    from homeassistant.components import panel_custom

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name="tactacam-photo-panel",
        module_url=module_url,
        require_admin=False,
    )
