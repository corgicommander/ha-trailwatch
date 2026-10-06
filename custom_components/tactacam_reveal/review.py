"""Review queue: step through "Needs review" photos and sort them by hand."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .archive import (
    FALSE_POSITIVES,
    NON_SIGHTING_FOLDERS,
    REFERENCE,
    UNCERTAIN,
    category_folder,
    move_to_category,
    sort_key,
)
from .const import DOMAIN, archive_location

if TYPE_CHECKING:
    from .coordinator import RevealCoordinator

_LOGGER = logging.getLogger(__name__)

# Offered in the label list even before any photo was sorted into them.
COMMON_LABELS = (
    "bird", "cat", "coyote", "deer", "dog", "fox", "human", "opossum",
    "rabbit", "raccoon", "squirrel", "turkey", "turtle", "vehicle",
)
NOTES_LIMIT = 3000


def list_review(root: Path) -> tuple[list[str], list[str], set[str]]:
    """(photos in every camera's 'uncertain' folder newest first, known labels,
    videos in those folders). Runs in the executor.
    """
    base = root.resolve()
    if not base.is_dir():
        return [], list(COMMON_LABELS), set()
    photos = [
        p.relative_to(base).as_posix()
        for p in base.glob(f"*/{UNCERTAIN}/*_photo.jpg")
        if not p.parts[-3].startswith((".", "@"))
    ]
    videos = {
        p.relative_to(base).as_posix() for p in base.glob(f"*/{UNCERTAIN}/*_video.mp4")
    }
    photos.sort(key=lambda rel: sort_key(rel.rsplit("/", 1)[-1]), reverse=True)
    labels = set(COMMON_LABELS)
    for folder in base.glob("*/*"):
        if folder.is_dir() and folder.name not in NON_SIGHTING_FOLDERS and not folder.name.startswith("."):
            labels.update(folder.name.split("-"))
    return photos, sorted(labels), videos


class ReviewQueue:
    """The photo being reviewed, with what the classifier thought of it."""

    def __init__(self, hass: HomeAssistant, coordinator: RevealCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._store = Store(hass, 1, f"{DOMAIN}.{coordinator.entry.entry_id}.review")
        self.notes: dict[str, dict[str, Any]] = {}
        self.items: list[str] = []
        self.labels: list[str] = list(COMMON_LABELS)
        self.videos: set[str] = set()
        self.index = 0
        self.busy = False
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        self.notes = (await self._store.async_load() or {}).get("notes", {})
        await self.async_refresh()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_refresh(self) -> None:
        _, root = archive_location(self.hass)
        current = self.current
        self.items, self.labels, self.videos = await self.hass.async_add_executor_job(
            list_review, root
        )
        if current in self.items:
            self.index = self.items.index(current)
        self.index = min(self.index, max(len(self.items) - 1, 0))
        self._changed()

    @property
    def current(self) -> str | None:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def note(self, relative: str | None) -> dict[str, Any]:
        return self.notes.get(Path(relative).name, {}) if relative else {}

    @callback
    def async_record(self, relative: str, result: dict[str, Any]) -> None:
        """Remember why a photo went to review, for the review screen."""
        self.notes[Path(relative).name] = {
            "subject": result.get("subject") or "",
            "reason": result.get("reason") or "",
            "source": result.get("source") or "",
            "confidence": result.get("confidence") or "",
        }
        if len(self.notes) > NOTES_LIMIT:
            for name in list(self.notes)[: len(self.notes) - NOTES_LIMIT]:
                del self.notes[name]
        self._store.async_delay_save(lambda: {"notes": self.notes}, 10)

    def step(self, delta: int) -> None:
        if self.items:
            self.index = (self.index + delta) % len(self.items)
            self._changed()

    async def async_sort(self, label: str) -> str:
        """Move the current photo (and its video) to a label's folder."""
        relative = self._require()
        folder = REFERENCE if label == REFERENCE else category_folder(label)
        _, root = archive_location(self.hass)
        new_relative = await self.hass.async_add_executor_job(
            move_to_category, root, relative, folder
        )
        self.notes.pop(Path(relative).name, None)
        self._store.async_delay_save(lambda: {"notes": self.notes}, 10)
        await self.coordinator.async_refresh_review_counts()
        return new_relative

    async def async_ask_ai(self) -> None:
        """Ask the (stronger) AI about the current photo; sort it if sure."""
        relative = self._require()
        self.busy = True
        self._changed()
        try:
            result = await self.coordinator.classifier.async_ask_ai(relative)
        finally:
            self.busy = False
        if result.category not in (FALSE_POSITIVES, UNCERTAIN) or (
            result.category == FALSE_POSITIVES and result.confidence == "high"
        ):
            note = {"subject": result.subject, "reason": result.reason}
            await self.async_sort(result.category)
            _LOGGER.info("AI sorted %s into %s: %s", relative, result.category, note)
            return
        self.async_record(relative, result.as_dict())
        self._changed()

    def _require(self) -> str:
        relative = self.current
        if relative is None:
            raise ValueError("Nothing to review")
        return relative
