from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path
import re
from urllib.parse import urlparse

from homeassistant.components import persistent_notification
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RevealApi
from .alerts import AlertPolicy
from .archive import (
    UNCERTAIN,
    delete_old_false_positives,
    find_photos,
    media_id_for,
    media_url_path,
    move_to_category,
    review_counts,
)
from .classifier import Classification, MediaClassifier
from .health import ClassifierHealth
from .review import ReviewQueue
from .stats import TactacamStats
from .videos import VideoManager
from .const import (
    CONF_AUTO_CLASSIFY,
    CONF_CAMERA_CLASSIFY,
    CONF_CAMERAS,
    CONF_DOWNLOAD_PHOTOS,
    CONF_FALSE_POSITIVE_RETENTION_DAYS,
    DEFAULT_BACKLOG_DAYS,
    CONF_PERSISTENT_NOTIFICATIONS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    EVENT_MEDIA_CLASSIFIED,
    EVENT_NEW_MEDIA,
    NOTIFICATION_LINK_DAYS,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
    archive_location,
    locate_media,
)

_LOGGER = logging.getLogger(__name__)


def _write_atomic(directory: Path, filename: str, content: bytes) -> None:
    directory.mkdir(0o755, True, True)
    temporary = directory / f"{filename}.part"
    temporary.write_bytes(content)
    temporary.replace(directory / filename)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip()).strip("._")
    return value or "reveal_camera"


SEEN_LIMIT = 20000


class RevealCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self, hass: HomeAssistant, api: RevealApi, entry: ConfigEntry
    ) -> None:
        self.api = api
        self.entry = entry
        # Use a new key for the gallery archive index. This intentionally
        # triggers a clean backfill without requiring migration of the older
        # latest-image-only store.
        self._store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.archive_v3"
        )
        # Ordered oldest-first. Keys are "<media id>:<photo|video>"; plain ids
        # from releases before 0.9 mean the photo was handled.
        self._seen: dict[str, None] = {}
        self._known_cameras: set[str] = set()
        self._loaded = False
        self._archive_task: asyncio.Task | None = None
        options = {**entry.data, **entry.options}
        self.options = options
        self.classifier = MediaClassifier(hass, options)
        self.alerts = AlertPolicy(hass, options)
        self.stats = TactacamStats(hass, entry.entry_id)
        self.health = ClassifierHealth(
            hass, self.classifier.url, self.classifier.token
        )
        self.classifier.on_ai_result = self.health.async_ai_result
        self._auto_classify = options.get(CONF_AUTO_CLASSIFY, True)
        self._batch_task: asyncio.Task | None = None
        self.backlog_days = DEFAULT_BACKLOG_DAYS
        self.batch_total = 0
        self.batch_done = 0
        self.review_counts: dict[str, int] = {}
        self._progress_listeners: list = []
        self.videos = VideoManager(hass, self)
        self.review = ReviewQueue(hass, self)
        minutes = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, UPDATE_INTERVAL_MINUTES),
            )
        )
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )

    def _option(self, key: str, default):
        return self.options.get(key, default)

    async def async_start_services(self) -> None:
        """Load stats, start health checks and the daily clean-up."""
        await self.stats.async_load()
        await self.health.async_start()
        await self.videos.async_start()
        await self.review.async_load()
        await self.async_refresh_review_counts()
        self.entry.async_on_unload(
            async_track_time_change(
                self.hass, self._async_daily_cleanup, hour=3, minute=30, second=0
            )
        )

    def async_cancel_tasks(self) -> None:
        """Stop background work when the integration unloads."""
        self.health.async_stop()
        self.videos.async_stop()
        for task in (self._archive_task, self._batch_task):
            if task is not None and not task.done():
                task.cancel()

    async def _async_update_data(self) -> dict:
        try:
            if not self._loaded:
                stored = await self._store.async_load() or {}
                self._seen = dict.fromkeys(stored.get("seen", []))
                self._known_cameras = set(stored.get("cameras", []))
                self._loaded = True

            cameras = await self.api.async_cameras()
            if self._seen and not self._known_cameras:
                # Store from before 0.9: every current camera was already synced.
                self._known_cameras = {str(c["cameraId"]) for c in cameras}
            result = {}
            for camera in cameras:
                camera_id = str(camera["cameraId"])
                result[camera_id] = {
                    "camera": camera,
                    "photo": (self.data or {}).get(camera_id, {}).get("photo"),
                }

            # Photo/video API calls and archive downloads can be slow. Never
            # make Home Assistant wait for them while setting up the entities.
            if self._archive_task is None or self._archive_task.done():
                self._archive_task = self.hass.async_create_task(
                    self._async_fetch_and_archive(cameras),
                    "Archive Tactacam Reveal media",
                )
            return result
        except Exception as err:
            _LOGGER.exception("Unable to update Reveal camera list")
            raise UpdateFailed(
                f"Unable to update Reveal cameras: {type(err).__name__}: {err!r}"
            ) from err

    async def _async_fetch_and_archive(self, cameras: list[dict]) -> None:
        media_results = await asyncio.gather(
            *(
                self.api.async_recent_media(str(camera["cameraId"]))
                for camera in cameras
            ),
            return_exceptions=True,
        )
        latest_changed = False
        changed = False
        for camera, media_result in zip(cameras, media_results, strict=False):
            camera_id = str(camera["cameraId"])
            if isinstance(media_result, BaseException):
                _LOGGER.error(
                    "Unable to fetch Reveal media for camera %s: %r",
                    camera_id,
                    media_result,
                )
                continue
            latest = next(
                (item for item in media_result if item.get("photoUrl")), None
            )
            if latest and self.data and camera_id in self.data:
                self.data[camera_id]["photo"] = latest
                latest_changed = True
            # A camera's first successful sync archives its history silently.
            initial_sync = camera_id not in self._known_cameras
            for item in reversed(media_result):
                if await self._async_process_media(camera, item, initial_sync):
                    changed = True
            self._known_cameras.add(camera_id)
            changed = changed or initial_sync
        if latest_changed and self.data:
            self.async_set_updated_data(self.data)
        try:
            await self.videos.async_sync()
        except Exception:
            _LOGGER.exception("Unable to sync Reveal videos")
        if changed:
            keys = list(self._seen)[-SEEN_LIMIT:]
            self._seen = dict.fromkeys(keys)
            await self._store.async_save(
                {"seen": keys, "cameras": sorted(self._known_cameras)}
            )

    async def _async_process_media(
        self, camera: dict, item: dict, initial_sync: bool
    ) -> bool:
        """Archive each wanted file of one Reveal record once. Returns True if
        anything new was saved.

        Photos and videos are tracked separately, so a failed video does not
        repeat the photo's event and a video that appears later is still
        fetched. Disabled media types are not marked as seen, so enabling
        them later fills the gap.
        """
        media_id = str(item.get("photoId") or item.get("id") or "")
        if not media_id:
            return False
        legacy = media_id in self._seen
        candidates = []
        if self._option(CONF_DOWNLOAD_PHOTOS, True) and item.get("photoUrl"):
            candidates.append(("photo", item["photoUrl"], ".jpg"))
        # Photo records never carry a video: requested videos are listed
        # separately and handled by VideoManager.

        captured = self._capture_timestamp(item)
        camera_name = camera.get("name") or str(camera.get("cameraId"))
        folder = _safe_name(camera_name)
        saved = False
        for media_type, url, extension in candidates:
            key = f"{media_id}:{media_type}"
            if key in self._seen or (legacy and media_type == "photo"):
                continue
            source_id, archive_root = archive_location(self.hass)
            filename = f"{_safe_name(str(captured))}_{_safe_name(media_id)}_{media_type}{extension}"
            relative = f"{folder}/{filename}"
            try:
                existing = await self.hass.async_add_executor_job(
                    locate_media, archive_root, relative
                )
                if existing is None:
                    content = await self.api.async_image(url)
                    await self.hass.async_add_executor_job(
                        _write_atomic, archive_root / folder, filename, content
                    )
            except Exception:
                _LOGGER.exception("Unable to archive Reveal %s %s", media_type, media_id)
                continue
            self._seen[key] = None
            saved = True
            # Videos found for records handled before 0.9 are backfill: quiet.
            if initial_sync or (legacy and media_type == "video"):
                continue
            event_data = {
                "camera_id": str(camera.get("cameraId")),
                "camera_name": camera_name,
                "media_type": media_type,
                "captured_at": captured,
                "local_path": str(archive_root / relative),
                "media_content_id": f"media-source://media_source/{source_id}/{DOMAIN}/{relative}",
                "photo_id": media_id,
                "hd_photo": bool(item.get("hdPhoto")),
            }
            self.hass.bus.async_fire(EVENT_NEW_MEDIA, event_data)
            if (
                media_type == "photo"
                and self._auto_classify
                and self.classifier.enabled
                and self.camera_classifies(folder)
            ):
                self.hass.async_create_background_task(
                    self._async_auto_classify(relative, event_data),
                    f"Classify Tactacam Reveal {filename}",
                )
            if self._option(CONF_PERSISTENT_NOTIFICATIONS, False):
                persistent_notification.async_create(
                    self.hass,
                    f"New {media_type} from {camera_name}",
                    title="Tactacam Reveal",
                    notification_id=f"tactacam_reveal_{media_id}_{media_type}",
                )
        return saved

    def camera_classifies(self, folder: str) -> bool:
        camera = (self.options.get(CONF_CAMERAS) or {}).get(folder) or {}
        return camera.get(CONF_CAMERA_CLASSIFY, True)

    def camera_folder(self, camera_id: str) -> str:
        camera = (self.data or {}).get(camera_id, {}).get("camera") or {}
        return _safe_name(camera.get("name") or camera_id)

    def media_id(self, relative: str) -> str:
        return media_id_for(self.hass, relative)

    async def async_refresh_review_counts(self) -> None:
        _, root = archive_location(self.hass)
        self.review_counts = await self.hass.async_add_executor_job(review_counts, root)
        await self.review.async_refresh()
        self._notify_progress()

    async def _async_daily_cleanup(self, _now=None) -> None:
        days = int(self._option(CONF_FALSE_POSITIVE_RETENTION_DAYS, 0) or 0)
        if days <= 0:
            return
        _, root = archive_location(self.hass)
        removed = await self.hass.async_add_executor_job(
            delete_old_false_positives, root, days, dt_util.now().date().isoformat()
        )
        if removed:
            _LOGGER.info("Deleted %s empty Reveal photos older than %s days", removed, days)

    async def async_classify_archive(
        self,
        camera_folder: str | None,
        since: str | None,
        until: str | None,
        include_sorted: bool,
    ) -> int:
        """Classify and sort archived photos in the background; return how many."""
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("A Tactacam archive classification is already running")
        _, root = archive_location(self.hass)
        photos = await self.hass.async_add_executor_job(
            find_photos, root, camera_folder, since, until, include_sorted
        )
        self._batch_task = self.hass.async_create_background_task(
            self._async_run_batch(photos), "Classify Tactacam Reveal archive"
        )
        return len(photos)

    def async_cancel_batch(self) -> None:
        if self.batch_running:
            self._batch_task.cancel()

    @property
    def batch_running(self) -> bool:
        return self._batch_task is not None and not self._batch_task.done()

    def async_add_progress_listener(self, listener) -> callable:
        self._progress_listeners.append(listener)
        return lambda: self._progress_listeners.remove(listener)

    def _notify_progress(self) -> None:
        for listener in list(self._progress_listeners):
            listener()

    async def _async_run_batch(self, photos: list[str]) -> None:
        self.batch_total, self.batch_done = len(photos), 0
        self._notify_progress()
        try:
            await self._async_classify_batch(photos)
        finally:
            self._notify_progress()

    async def _async_classify_batch(self, photos: list[str]) -> None:
        categories: dict[str, int] = {}
        sources: dict[str, int] = {}
        failed = 0
        for relative in photos:
            try:
                result = await self.async_classify_and_publish(
                    relative,
                    {"camera_name": relative.split("/", 1)[0].replace("_", " ")},
                    fire_event=False,
                )
            except Exception:  # Keep going; the photo stays where it was.
                _LOGGER.exception("Unable to classify Reveal media %s", relative)
                failed += 1
                continue
            finally:
                self.batch_done += 1
                self._notify_progress()
            categories[result["category"]] = categories.get(result["category"], 0) + 1
            sources[result["source"]] = sources.get(result["source"], 0) + 1
        summary = ", ".join(f"{k} ({v})" for k, v in sorted(categories.items()))
        decided = ", ".join(f"{k} ({v})" for k, v in sorted(sources.items()))
        persistent_notification.async_create(
            self.hass,
            f"Classified {len(photos) - failed} of {len(photos)} photos.\n\n"
            f"**Decided by:** {decided or 'nothing'}\n\n**Sorted into:** {summary or 'nothing'}",
            title="Tactacam Reveal archive classified",
            notification_id="tactacam_reveal_archive_classified",
        )

    async def _async_auto_classify(self, relative: str, event_data: dict) -> None:
        try:
            await self.async_classify_and_publish(relative, event_data, live=True)
        except Exception:
            # The photo stays archived, unsorted, for a later re-run.
            _LOGGER.exception("Unable to classify Reveal media %s", relative)

    async def async_classify_and_publish(
        self,
        relative: str,
        event_data: dict | None = None,
        *,
        sort: bool = True,
        fire_event: bool = True,
        live: bool = False,
    ) -> dict:
        """Classify an archived photo, optionally sort it, and announce the result.

        Only live (newly arrived) photos update sightings and can notify.
        """
        folder = relative.split("/", 1)[0]
        result = await self.classifier.async_classify(relative)
        if sort:
            _, root = archive_location(self.hass)
            relative = await self.hass.async_add_executor_job(
                move_to_category, root, relative, result.category
            )
        data = self._result_data(relative, result, event_data)
        if result.category == UNCERTAIN:
            self.review.async_record(relative, data)
        self.stats.async_record(folder, data, live=live)
        if live:
            data["notify"], data["suppressed"] = self.alerts.decide(folder, data)
            if data["notify"]:
                await self.alerts.async_send(data)
        else:
            data["notify"], data["suppressed"] = False, "not a live photo"
        if result.category == UNCERTAIN or sort:
            await self.async_refresh_review_counts()
        if fire_event:
            self.hass.bus.async_fire(EVENT_MEDIA_CLASSIFIED, data)
        if live and (event_data or {}).get("photo_id") and not (event_data or {}).get("hd_photo"):
            name = relative.rsplit("/", 1)[-1]
            if self.videos.wants_video(name, data.get("labels") or [], result.category):
                await self.videos.async_consider(
                    {
                        "photo_id": event_data["photo_id"],
                        "camera_id": event_data.get("camera_id"),
                        "relative": relative,
                        "labels": data.get("labels") or [],
                    }
                )
        return data

    async def async_publish_video(
        self,
        photo: str,
        result: Classification,
        event_data: dict,
        *,
        category: str,
        labels: list[str],
        live: bool,
    ) -> None:
        """Announce a classified video. `photo` is its (possibly moved) photo,
        `category`/`labels` the combined verdict of photo and video."""
        folder = photo.split("/", 1)[0]
        data = self._result_data(photo, result, event_data)
        data.update(category=category, labels=labels, media_type="video")
        data["video_content_id"] = self.media_id(
            photo.removesuffix("_photo.jpg") + "_video.mp4"
        )
        if category == UNCERTAIN:
            self.review.async_record(photo, data)
        self.stats.async_record(folder, data, live=live)
        if live:
            data["notify"], data["suppressed"] = self.alerts.decide(folder, data)
            if data["notify"]:
                await self.alerts.async_send(data)
        else:
            data["notify"], data["suppressed"] = False, "nothing new in the video"
        await self.async_refresh_review_counts()
        self.hass.bus.async_fire(EVENT_MEDIA_CLASSIFIED, data)

    def _result_data(
        self, relative: str, result: Classification, event_data: dict | None
    ) -> dict:
        data = {
            **(event_data or {}),
            **result.as_dict(),
            "relative": relative,
            "media_content_id": media_id_for(self.hass, relative),
            "image_url": self._absolute_url(
                async_sign_path(
                    self.hass,
                    media_url_path(self.hass, relative),
                    timedelta(days=NOTIFICATION_LINK_DAYS),
                    use_content_user=True,
                )
            ),
        }
        data.pop("local", None)
        data.pop("local_path", None)
        return data

    def _absolute_url(self, path: str) -> str:
        """Full external URL for a signed path.

        The iOS app escapes the '?' of a relative link, which breaks the
        signature, so notifications only show the photo with a full URL.
        """
        try:
            base = get_url(self.hass, prefer_external=True, prefer_cloud=True)
        except NoURLAvailableError:
            return path
        return f"{base}{path}"

    @staticmethod
    def _capture_timestamp(item: dict) -> str:
        for key in (
            "createdAt",
            "dateCreated",
            "capturedAt",
            "captureDate",
            "created",
            "date",
            "timestamp",
        ):
            if item.get(key):
                return str(item[key]).replace(":", "-")

        # Reveal filenames contain MMDDYYYYHHMMSS, for example
        # 08012026133115. This is reliable even when the JSON omits a date.
        for url_key in ("photoUrl", "videoUrl"):
            if not item.get(url_key):
                continue
            filename = Path(urlparse(item[url_key]).path).name
            match = re.search(r"-(\d{14})-", filename)
            if match:
                value = match.group(1)
                return (
                    f"{value[4:8]}-{value[0:2]}-{value[2:4]}_"
                    f"{value[8:10]}-{value[10:12]}-{value[12:14]}"
                )
        return "undated"
