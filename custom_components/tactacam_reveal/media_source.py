from __future__ import annotations

import mimetypes
from pathlib import Path
import re

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .archive import NON_SIGHTING_FOLDERS, all_sightings, pretty_category
from .const import DOMAIN, archive_location, locate_media

ALL_SIGHTINGS = "__all_sightings__"
ALL_SIGHTINGS_LIMIT = 500


def _newest_first_key(entry: Path) -> str:
    """Sort by the Reveal capture time in the name (MMDDYYYYHHMMSS)."""
    match = re.search(r"-(\d{2})(\d{2})(\d{4})(\d{6})-", entry.name)
    if match:
        month, day, year, clock = match.groups()
        return f"{year}{month}{day}{clock}"
    return entry.name


async def async_get_media_source(hass: HomeAssistant) -> "RevealMediaSource":
    return RevealMediaSource(hass)


class RevealMediaSource(MediaSource):
    name = "Tactacam Reveal"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _path(self, identifier: str | None) -> Path:
        _, root = archive_location(self.hass)
        root = root.resolve()
        candidate = (root / (identifier or "")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as err:
            raise Unresolvable("Invalid Reveal media path") from err
        return candidate

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        source_id, root = archive_location(self.hass)
        path = await self.hass.async_add_executor_job(
            locate_media, root, item.identifier or ""
        )
        if path is None:
            raise Unresolvable("Reveal media file no longer exists")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        relative = path.relative_to(root.resolve()).as_posix()
        return PlayMedia(
            f"/media/{source_id}/tactacam_reveal/{relative}",
            mime_type,
            path=path,
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        identifier = item.identifier or ""
        if identifier == ALL_SIGHTINGS:
            return await self.hass.async_add_executor_job(self._build_all_sightings)
        path = self._path(identifier)
        if not await self.hass.async_add_executor_job(path.exists):
            if not identifier:
                await self.hass.async_add_executor_job(path.mkdir, 0o755, True, True)
            else:
                raise BrowseError("Reveal media folder does not exist")
        return await self.hass.async_add_executor_job(self._build_item, path, identifier)

    def _file_item(self, path: Path, identifier: str, title: str | None = None):
        mime_type = mimetypes.guess_type(path.name)[0]
        if not mime_type or not mime_type.startswith(("image/", "video/")):
            return None
        media_class = MediaClass.VIDEO if mime_type.startswith("video/") else MediaClass.IMAGE
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=media_class,
            media_content_type=mime_type,
            title=title or path.name,
            can_play=True,
            can_expand=False,
            thumbnail=(
                f"/media/{archive_location(self.hass)[0]}/{DOMAIN}/{identifier}"
                if media_class == MediaClass.IMAGE
                else None
            ),
        )

    def _folder_item(self, identifier: str, title: str, thumbnail: str | None = None):
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            thumbnail=thumbnail,
        )

    def _build_all_sightings(self) -> BrowseMediaSource:
        _, root = archive_location(self.hass)
        children = []
        for relative in all_sightings(root, ALL_SIGHTINGS_LIMIT):
            camera, folder, name = relative.split("/", 2)
            child = self._file_item(
                Path(name), relative,
                f"{camera.replace('_', ' ')} · {pretty_category(folder)} · {name[:16].replace('_', ' ')}",
            )
            if child:
                children.append(child)
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=ALL_SIGHTINGS,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title="All sightings",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.IMAGE,
        )

    def _build_item(self, path: Path, identifier: str) -> BrowseMediaSource:
        if path.is_file():
            return self._file_item(path, identifier)
        depth = identifier.count("/") + 1 if identifier else 0
        children = []
        if depth == 0:
            # Root: all sightings across cameras, then one folder per camera.
            children.append(self._folder_item(ALL_SIGHTINGS, "All sightings"))
            for entry in sorted(
                (e for e in path.iterdir() if e.is_dir() and not e.name.startswith((".", "@"))),
                key=lambda e: e.name,
            ):
                children.append(self._folder_item(entry.name, entry.name.replace("_", " ")))
        elif depth == 1:
            # Camera: sighting folders first, then review/empty/reference, then
            # photos not sorted yet. Folder titles carry their photo counts.
            folders = [e for e in path.iterdir() if e.is_dir() and not e.name.startswith((".", "@"))]
            folders.sort(key=lambda e: (e.name in NON_SIGHTING_FOLDERS, e.name))
            for entry in folders:
                count = sum(1 for _ in entry.rglob("*_photo.jpg"))
                children.append(
                    self._folder_item(
                        f"{identifier}/{entry.name}",
                        f"{pretty_category(entry.name)} ({count})",
                    )
                )
            files = sorted(
                (e for e in path.iterdir() if e.is_file()),
                key=_newest_first_key,
                reverse=True,
            )
            children += [
                c for e in files if (c := self._file_item(e, f"{identifier}/{e.name}"))
            ]
        elif depth == 2 and path.is_dir():
            files = sorted(
                (e for e in path.rglob("*") if e.is_file() and not e.name.startswith(".")),
                key=_newest_first_key,
                reverse=True,
            )
            children += [
                c
                for e in files
                if (c := self._file_item(e, f"{identifier}/{e.relative_to(path).as_posix()}"))
            ]
        title = "Tactacam Reveal"
        if depth == 1:
            title = path.name.replace("_", " ")
        elif depth == 2:
            title = pretty_category(path.name)
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier or None,
            media_class=MediaClass.APP if not identifier else MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )
