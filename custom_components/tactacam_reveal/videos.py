"""Reveal videos: download uploaded clips, pair them with their photo, classify
them, and request clips automatically one at a time.

Reveal cameras record video but only upload a still (file names with "-V-").
The clip is uploaded when someone requests it, and Reveal handles one request
at a time: a request sent while another is outstanding is skipped. Requests
therefore wait in a queue; the next is sent once the previous clip arrived or
timed out.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from aiohttp import ClientResponseError

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .archive import FALSE_POSITIVES, NON_SIGHTING_FOLDERS, UNCERTAIN, category_folder, move_to_category
from .const import (
    CONF_AUTO_REQUEST_VIDEOS,
    CONF_DOWNLOAD_VIDEOS,
    CONF_PERSISTENT_NOTIFICATIONS,
    CONF_VIDEO_REQUEST_LABELS,
    DOMAIN,
    EVENT_NEW_MEDIA,
    VIDEO_POLL_SECONDS,
    VIDEO_QUEUE_LIMIT,
    VIDEO_QUEUE_MAX_AGE_HOURS,
    VIDEO_REQUEST_RETRIES,
    VIDEO_REQUEST_TIMEOUT_MINUTES,
    archive_location,
    locate_media,
)

if TYPE_CHECKING:
    from .coordinator import RevealCoordinator

_LOGGER = logging.getLogger(__name__)

SEEN_LIMIT = 5000
FRAMES_FOLDER = ".frames"


def reveal_time(value: str) -> datetime | None:
    """Parse Reveal's MMDDYYYYHHMMSS (camera local time, kept naive)."""
    match = re.fullmatch(r"(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})", value or "")
    if not match:
        return None
    month, day, year, hour, minute, second = map(int, match.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _name_time(name: str) -> datetime | None:
    match = re.search(r"-(\d{14})-", name)
    return reveal_time(match.group(1)) if match else None


def _clip_number(name: str) -> str:
    """Trailing digits of a Reveal file name, e.g. SYFW02411 -> 02411."""
    match = re.search(r"(\d+)(?:\.\w+)?$", name.removesuffix("_photo.jpg"))
    return match.group(1)[-5:] if match else ""


def find_video_photo(
    root: Path, camera_folder: str, video_id: str, when: datetime | None
) -> str | None:
    """Archived photo (relative path) that a manually requested video belongs to.

    Runs in the executor. Only video stills ("-V-") without a video yet are
    candidates. Reveal stamps a requested clip with its upload time, so a
    matching clip number wins (the newest such still, as numbers eventually
    wrap); otherwise the nearest still within five minutes of the clip's time.
    """
    base = root.resolve()
    camera_dir = base / camera_folder
    if not camera_dir.is_dir():
        return None
    number = _clip_number(video_id)
    by_number: list[tuple[datetime, Path]] = []
    best: tuple[float, Path] | None = None
    for photo in camera_dir.rglob("*-V-*_photo.jpg"):
        if FRAMES_FOLDER in photo.parts:
            continue
        if photo.with_name(photo.name.removesuffix("_photo.jpg") + "_video.mp4").exists():
            continue
        taken = _name_time(photo.name)
        if taken is None:
            continue
        if number and _clip_number(photo.name) == number:
            by_number.append((taken, photo))
            continue
        if when is not None:
            gap = abs((when - taken).total_seconds())
            if gap <= 300 and (best is None or gap < best[0]):
                best = (gap, photo)
    if by_number:
        return max(by_number)[1].relative_to(base).as_posix()
    return best[1].relative_to(base).as_posix() if best else None


def loose_videos(root: Path, min_age_seconds: int, limit: int) -> list[tuple[str, str | None]]:
    """Videos left unsorted in a camera folder (classification failed or was
    interrupted), oldest first, with their photo if it is next to them.

    Runs in the executor. Recent files are skipped: they may still be in work.
    """
    import time

    base = root.resolve()
    if not base.is_dir():
        return []
    cutoff = time.time() - min_age_seconds
    found = []
    for video in base.glob("*/*_video.mp4"):
        if video.parent.name.startswith((".", "@")) or video.stat().st_mtime > cutoff:
            continue
        photo = video.with_name(video.name.removesuffix("_video.mp4") + "_photo.jpg")
        found.append((
            video.relative_to(base).as_posix(),
            photo.relative_to(base).as_posix() if photo.exists() else None,
        ))
    return sorted(found)[:limit]


async def _async_ffmpeg_frame(hass: HomeAssistant, video: Path) -> bytes | None:
    """A JPEG frame from inside a clip via Home Assistant's ffmpeg, or None."""
    from homeassistant.components.ffmpeg import async_get_image

    for offset in (5, 1):  # Reveal clips are ~10-20 s; fall back for short ones.
        try:
            image = await async_get_image(hass, str(video), extra_cmd=f"-ss {offset}")
        except Exception as err:  # ffmpeg missing or the file is unreadable
            _LOGGER.warning("Unable to take a frame from %s: %r", video.name, err)
            return None
        if image:
            return image
    return None


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(content)
    temporary.replace(path)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _folder_of(relative: str) -> str | None:
    """Category folder of an archived file, or None when it is unsorted."""
    parts = relative.split("/")
    return parts[1] if len(parts) > 2 else None


class VideoManager:
    """Video downloads, pairing, classification and the request queue."""

    def __init__(self, hass: HomeAssistant, coordinator: RevealCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        options = coordinator.options
        self.enabled = options.get(CONF_DOWNLOAD_VIDEOS, True)
        self.auto_request = bool(options.get(CONF_AUTO_REQUEST_VIDEOS, False))
        self.request_labels = {
            item.strip().lower()
            for item in (options.get(CONF_VIDEO_REQUEST_LABELS) or "").split(",")
            if item.strip()
        }
        self._store = Store(hass, 1, f"{DOMAIN}.{coordinator.entry.entry_id}.videos")
        self._seen: dict[str, None] = {}
        self._synced = False
        self.queue: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.last_result: str | None = None
        self._lock = asyncio.Lock()
        self._pump_lock = asyncio.Lock()
        self._backfill: asyncio.Task | None = None
        self._listeners: list = []

    # ----------------------------------------------------------------- setup

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self._seen = dict.fromkeys(stored.get("seen", []))
        self._synced = stored.get("synced", False)
        self.queue = stored.get("queue", [])
        self.pending = stored.get("pending")
        self.coordinator.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=VIDEO_POLL_SECONDS)
            )
        )

    def async_stop(self) -> None:
        if self._backfill is not None and not self._backfill.done():
            self._backfill.cancel()

    @callback
    def async_add_listener(self, listener) -> callable:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed(self) -> None:
        self._store.async_delay_save(self._data, 5)
        for listener in list(self._listeners):
            listener()

    def _data(self) -> dict[str, Any]:
        return {
            "seen": list(self._seen)[-SEEN_LIMIT:],
            "synced": self._synced,
            "queue": self.queue,
            "pending": self.pending,
        }

    # ------------------------------------------------------------- downloads

    async def async_sync(self) -> None:
        """Fetch newly uploaded videos (called on every regular update)."""
        if not self.enabled:
            return
        if not self._synced:
            # First run: archive every earlier video quietly, in the background.
            if self._backfill is None or self._backfill.done():
                self._backfill = self.hass.async_create_background_task(
                    self._async_backfill(), "Archive earlier Tactacam Reveal videos"
                )
            return
        await self._async_fetch(size=50)
        await self._async_retry_loose()

    async def _async_retry_loose(self) -> None:
        """Classify clips that an earlier attempt left unsorted."""
        if not self.coordinator.classifier.enabled:
            return
        _, root = archive_location(self.hass)
        leftovers = await self.hass.async_add_executor_job(loose_videos, root, 600, 10)
        async with self._lock:
            for relative, photo in leftovers:
                folder = relative.split("/", 1)[0]
                if not self.coordinator.camera_classifies(folder):
                    continue
                _LOGGER.info("Retrying classification of unsorted video %s", relative)
                event_data = {
                    "camera_name": folder.replace("_", " "),
                    "media_type": "video",
                    "media_content_id": self.coordinator.media_id(relative),
                }
                try:
                    await self._async_classify(relative, photo, event_data, live=False)
                except Exception:
                    _LOGGER.exception("Unable to classify Reveal video %s", relative)

    async def _async_backfill(self) -> None:
        videos: list[dict] = []
        for page in range(50):
            batch = await self.coordinator.api.async_videos(page=page, size=100)
            videos.extend(batch)
            if len(batch) < 100:
                break
        async with self._lock:
            for video in reversed(videos):
                await self._async_handle(video, quiet=True)
            self._synced = True
            self._changed()
        _LOGGER.info("Archived %s earlier Reveal videos", len(videos))

    async def _async_fetch(self, size: int) -> None:
        try:
            videos = await self.coordinator.api.async_videos(size=size)
        except Exception as err:  # Reveal hiccups: try again next time.
            _LOGGER.warning("Unable to list Reveal videos: %r", err)
            return
        async with self._lock:
            for video in reversed(videos):
                await self._async_handle(video, quiet=False)

    async def _async_handle(self, video: dict, *, quiet: bool) -> None:
        video_id = str(video.get("videoId") or "")
        url = video.get("videoUrl")
        if not video_id or not url or video_id in self._seen:
            return
        camera_id = str(video.get("cameraId") or "")
        camera = (self.coordinator.data or {}).get(camera_id, {}).get("camera") or {}
        camera_name = camera.get("name") or camera_id
        folder = self.coordinator.camera_folder(camera_id)
        _, root = archive_location(self.hass)
        when = reveal_time(str(video.get("videoTimestamp") or ""))

        requested = self._requested_photo(camera_id, video, when)
        if requested:
            # The photo may have been sorted (or reviewed) since it was queued.
            found = await self.hass.async_add_executor_job(locate_media, root, requested)
            requested = found.relative_to(root.resolve()).as_posix() if found else None
        photo = requested or await self.hass.async_add_executor_job(
            find_video_photo, root, folder, video_id, when
        )
        if photo:
            relative = photo.removesuffix("_photo.jpg") + "_video.mp4"
        else:
            stamp = when.strftime("%Y-%m-%d_%H-%M-%S") if when else "undated"
            safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", video_id)
            relative = f"{folder}/{stamp}_{safe_id}_video.mp4"
        try:
            content = await self.coordinator.api.async_image(url)
            await self.hass.async_add_executor_job(_write, root / relative, content)
        except Exception:
            _LOGGER.exception("Unable to archive Reveal video %s", video_id)
            return
        self._seen[video_id] = None
        if requested:
            self.pending = None
            self.last_result = f"Received the video for {Path(photo).name}"
        self._changed()

        event_data = {
            "camera_id": camera_id,
            "camera_name": camera_name,
            "media_type": "video",
            "captured_at": when.isoformat() if when else None,
            "local_path": str(root / relative),
            "media_content_id": self.coordinator.media_id(relative),
            "photo_id": video_id,
            "paired_photo": photo,
        }
        if not quiet:
            self.hass.bus.async_fire(EVENT_NEW_MEDIA, event_data)
            if self.coordinator.options.get(CONF_PERSISTENT_NOTIFICATIONS, False):
                persistent_notification.async_create(
                    self.hass,
                    f"New video from {camera_name}",
                    title="Tactacam Reveal",
                    notification_id=f"tactacam_reveal_{video_id}_video",
                )
        if self.coordinator.classifier.enabled and self.coordinator.camera_classifies(folder):
            try:
                await self._async_classify(relative, photo, event_data, live=not quiet)
            except Exception:
                _LOGGER.exception("Unable to classify Reveal video %s", relative)
        await self._async_pump()

    def _requested_photo(
        self, camera_id: str, video: dict, when: datetime | None
    ) -> str | None:
        """The photo whose video we asked for, if this upload answers it."""
        pending = self.pending
        if not pending or pending.get("camera_id") != camera_id:
            return None
        created = video.get("createdTimestamp") or 0
        if created and created < pending.get("requested_at", 0) - 60_000:
            return None  # Uploaded before our request: someone else's.
        # Reveal stamps a requested clip with its upload time, not its capture
        # time, so the clip number is what ties it to the photo.
        video_number = _clip_number(str(video.get("videoId") or ""))
        photo_number = _clip_number(Path(pending["relative"]).name)
        if video_number and photo_number and video_number != photo_number:
            return None  # Another clip (requested in the Reveal app).
        return pending["relative"]

    # -------------------------------------------------------- classification

    async def _async_classify(
        self, relative: str, photo: str | None, event_data: dict, *, live: bool
    ) -> None:
        """Classify a video; it can move its photo pair to a better folder."""
        coordinator = self.coordinator
        classifier = coordinator.classifier
        _, root = archive_location(self.hass)
        camera_folder = relative.split("/", 1)[0]
        local = await classifier.async_local_video(relative)
        frame_bytes = None
        if local and local.get("frame_jpeg"):
            frame_bytes = base64.b64decode(local["frame_jpeg"])
        elif classifier.ai_entity:
            # No local server (AI-only setups) or it is down: let Home
            # Assistant's ffmpeg take a frame for the AI to look at.
            frame_bytes = await _async_ffmpeg_frame(self.hass, root / relative)

        # Without a photo, the deciding frame becomes the video's photo so the
        # clip shows up in the gallery, the review screen and notifications.
        frame_relative = None
        temporary_frame = None
        if photo is None and frame_bytes:
            photo = relative.removesuffix("_video.mp4") + "_photo.jpg"
            await self.hass.async_add_executor_job(_write, root / photo, frame_bytes)
            frame_relative = photo
        elif frame_bytes:
            temporary_frame = f"{camera_folder}/{FRAMES_FOLDER}/{Path(relative).stem}.jpg"
            await self.hass.async_add_executor_job(_write, root / temporary_frame, frame_bytes)
            frame_relative = temporary_frame
        try:
            result = await classifier.async_decide_video(local, frame_relative)
        finally:
            if temporary_frame:
                await self.hass.async_add_executor_job(_unlink, root / temporary_frame)

        if photo is None:
            # No server answer and no photo: nothing to sort the clip with.
            coordinator.stats.async_record(camera_folder, result.as_dict(), live=False)
            return
        current = _folder_of(photo)
        before = set() if current in NON_SIGHTING_FOLDERS or current is None else set(current.split("-"))
        if result.category in (FALSE_POSITIVES, UNCERTAIN):
            # The video saw nothing new: keep the photo's verdict if it had one.
            target = current if current is not None else result.category
            labels = before
        else:
            labels = before | set(result.labels)
            target = category_folder("-".join(labels))
        if target != current:
            photo = await self.hass.async_add_executor_job(move_to_category, root, photo, target)
        # Only alert when the clip shows something its photo did not.
        await coordinator.async_publish_video(
            photo,
            result,
            event_data,
            category=target,
            labels=sorted(labels),
            live=live and bool(labels - before),
        )

    # --------------------------------------------------------------- requests

    def wants_video(self, photo_name: str, labels: list[str], category: str) -> bool:
        """Should this newly classified photo's video be requested?"""
        if not (self.enabled and self.auto_request) or "-V-" not in photo_name:
            return False
        if not self.request_labels:
            return True  # Blank list: every video.
        return bool(self.request_labels & ({*labels, *category.split("-")}))

    async def async_consider(self, item: dict[str, Any]) -> None:
        """Queue a video request for a live photo. `item` holds photo_id,
        camera_id, relative (photo path) and labels."""
        if any(q["photo_id"] == item["photo_id"] for q in self.queue) or (
            self.pending and self.pending["photo_id"] == item["photo_id"]
        ):
            return
        self.queue.append({**item, "queued_at": dt_util.utcnow().isoformat(), "tries": 0})
        # Keep the newest requests when a busy day overflows the queue.
        self.queue = self.queue[-VIDEO_QUEUE_LIMIT:]
        self._changed()
        await self._async_pump()

    async def _async_tick(self, _now=None) -> None:
        if self.pending:
            await self._async_fetch(size=10)
        await self._async_pump()

    async def _async_pump(self) -> None:
        """Send the next request when nothing is outstanding."""
        if self._pump_lock.locked():
            return
        async with self._pump_lock:
            await self._async_pump_locked()

    async def _async_pump_locked(self) -> None:
        now = dt_util.utcnow()
        if self.pending:
            requested = datetime.fromisoformat(self.pending["requested_iso"])
            if now - requested < timedelta(minutes=VIDEO_REQUEST_TIMEOUT_MINUTES):
                return
            _LOGGER.warning(
                "Reveal video for %s did not arrive in %s minutes; moving on",
                self.pending["photo_id"],
                VIDEO_REQUEST_TIMEOUT_MINUTES,
            )
            self.last_result = f"Timed out waiting for {self.pending['photo_id']}"
            self.pending = None
            self._changed()
        cutoff = now - timedelta(hours=VIDEO_QUEUE_MAX_AGE_HOURS)
        fresh = [q for q in self.queue if datetime.fromisoformat(q["queued_at"]) >= cutoff]
        if len(fresh) != len(self.queue):
            self.queue = fresh
            self._changed()
        if not self.queue:
            return  # Requests are only queued when wanted (automatic or by hand).
        item = self.queue[0]
        try:
            refused = await self.coordinator.api.async_request_video(item["photo_id"])
        except ClientResponseError as err:
            _LOGGER.warning("Reveal refused a video request for %s: %s", item["photo_id"], err)
            refused = 1
        except Exception as err:
            _LOGGER.warning("Unable to request Reveal video %s: %r", item["photo_id"], err)
            return  # Network trouble: try again next minute.
        if refused:
            item["tries"] += 1
            if item["tries"] >= VIDEO_REQUEST_RETRIES:
                self.queue.pop(0)
                self.last_result = f"Reveal refused the video request for {item['photo_id']}"
            self._changed()
            return
        self.queue.pop(0)
        self.pending = {
            **item,
            "requested_iso": now.isoformat(),
            "requested_at": int(now.timestamp() * 1000),
        }
        self.last_result = f"Requested the video for {item['photo_id']}"
        self._changed()
