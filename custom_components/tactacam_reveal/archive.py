"""Archive helpers: media IDs, category folders and moving files between them."""

from __future__ import annotations

from pathlib import Path
import re

from homeassistant.core import HomeAssistant

from .const import DOMAIN, archive_location, locate_media

FALSE_POSITIVES = "falsepositives"
UNCERTAIN = "uncertain"
REFERENCE = "reference"
# Folders from older releases whose photos still count as unsorted.
LEGACY_FOLDERS = {"unknown"}

LABEL_ALIASES = {
    "person": "human",
    "people": "human",
    "persons": "human",
    "humans": "human",
    "falsepositive": "",
    "falsepositives": "",
    "none": "",
    "nothing": "",
    "empty": "",
    "blank": "",
    # Words that wrap an answer rather than name a subject.
    "result": "",
    "value": "",
    "category": "",
    "answer": "",
}


def category_folder(category: str) -> str:
    """Normalize labels such as 'Person, Dog' into a folder name like 'dog-human'."""
    labels = sorted(
        {
            LABEL_ALIASES.get(label, label)
            for label in re.split(r"[^a-z0-9]+", category.lower())
            if label
        }
        - {""}
    )
    return "-".join(labels) or FALSE_POSITIVES


def relative_from_media_id(hass: HomeAssistant, media_content_id: str) -> str | None:
    """Return the archive-relative path for either media-source ID form."""
    source_id, _ = archive_location(hass)
    for prefix in (
        f"media-source://media_source/{source_id}/{DOMAIN}/",
        f"media-source://{DOMAIN}/",
    ):
        if media_content_id.startswith(prefix):
            return media_content_id[len(prefix):]
    return None


def media_id_for(hass: HomeAssistant, relative: str) -> str:
    source_id, _ = archive_location(hass)
    return f"media-source://media_source/{source_id}/{DOMAIN}/{relative}"


def media_url_path(hass: HomeAssistant, relative: str) -> str:
    """Path under Home Assistant's /media endpoint, before signing."""
    source_id, _ = archive_location(hass)
    return f"/media/{source_id}/{DOMAIN}/{relative}"


def move_to_category(root: Path, relative: str, folder: str) -> str:
    """Move a photo (and its video) into <camera>/<folder>/; return the new path.

    Runs in the executor. Raises FileNotFoundError when the photo is missing.
    """
    base = root.resolve()
    source = locate_media(base, relative)
    if source is None:
        raise FileNotFoundError(relative)
    camera_dir = base / source.relative_to(base).parts[0]
    target_dir = camera_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    old_dir = source.parent
    stem = source.name.removesuffix("_photo.jpg")
    for sibling in (source, source.with_name(f"{stem}_video.mp4")):
        if sibling.is_file() and sibling.parent != target_dir:
            sibling.replace(target_dir / sibling.name)
    # Drop category folders that the move left empty.
    if old_dir not in (camera_dir, target_dir) and not any(old_dir.iterdir()):
        old_dir.rmdir()
    return (target_dir / source.name).relative_to(base).as_posix()


def capture_date(name: str) -> str | None:
    """Return YYYY-MM-DD from a Reveal file name, or None when it has no date."""
    match = re.search(r"-(\d{2})(\d{2})(\d{4})\d{6}-", name)
    if match:
        month, day, year = match.groups()
        return f"{year}-{month}-{day}"
    match = re.match(r"(\d{4}-\d{2}-\d{2})_", name)
    return match.group(1) if match else None


def find_photos(
    root: Path,
    camera_folder: str | None,
    since: str | None,
    until: str | None,
    include_sorted: bool,
) -> list[str]:
    """List archive-relative photos to classify, oldest first. Runs in the executor.

    Without include_sorted only unsorted photos are listed: those loose in a
    camera folder or in a legacy folder. Reference photos are never listed.
    """
    base = root.resolve()
    cameras = [base / camera_folder] if camera_folder else [
        p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]
    found = []
    for camera_dir in cameras:
        if not camera_dir.is_dir():
            continue
        for photo in camera_dir.rglob("*_photo.jpg"):
            relative = photo.relative_to(base)
            folders = relative.parts[1:-1]
            if REFERENCE in folders or any(
                part.startswith((".", "@")) for part in relative.parts
            ):
                continue
            if not include_sorted and folders and folders[0] not in LEGACY_FOLDERS:
                continue
            date = capture_date(photo.name)
            if (since and (not date or date < since)) or (until and (not date or date > until)):
                continue
            found.append((date or "", relative.as_posix()))
    return [relative for _, relative in sorted(found)]


def camera_reference(root: Path, camera_folder: str) -> tuple[list[str], str]:
    """Return reference photos and scene notes stored in <camera>/reference/.

    Runs in the executor. Photos are returned archive-relative.
    """
    base = root.resolve()
    ref_dir = base / camera_folder / REFERENCE
    if not ref_dir.is_dir():
        return [], ""
    photos = sorted(
        p.relative_to(base).as_posix()
        for p in ref_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )[:2]
    notes_file = ref_dir / "scene.txt"
    notes = notes_file.read_text(encoding="utf-8").strip() if notes_file.is_file() else ""
    return photos, notes


# Folders that are not sightings of anything.
NON_SIGHTING_FOLDERS = {FALSE_POSITIVES, UNCERTAIN, REFERENCE, *LEGACY_FOLDERS}


def pretty_category(folder: str) -> str:
    """Readable folder title, e.g. 'dog-human' -> 'Dog & human'."""
    special = {
        FALSE_POSITIVES: "Empty (false positives)",
        UNCERTAIN: "Needs review",
        REFERENCE: "Reference photos",
        "unknown": "Unsorted (legacy)",
    }
    if folder in special:
        return special[folder]
    words = folder.replace("_", " ").split("-")
    text = " & ".join(words) if len(words) == 2 else ", ".join(words)
    return text[:1].upper() + text[1:]


def review_counts(root: Path) -> dict[str, int]:
    """Photos waiting in each camera's 'uncertain' folder. Runs in the executor."""
    base = root.resolve()
    if not base.is_dir():
        return {}
    return {
        camera.name: sum(1 for _ in (camera / UNCERTAIN).glob("*_photo.jpg"))
        for camera in base.iterdir()
        if camera.is_dir() and not camera.name.startswith((".", "@"))
        and (camera / UNCERTAIN).is_dir()
    }


def delete_old_false_positives(root: Path, days: int, today: str) -> int:
    """Delete empty-frame photos (and videos) captured more than `days` ago.

    `today` is YYYY-MM-DD. Runs in the executor. Returns how many photos went.
    """
    from datetime import date, timedelta

    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    base = root.resolve()
    removed = 0
    for folder in base.glob(f"*/{FALSE_POSITIVES}"):
        for photo in folder.glob("*_photo.jpg"):
            captured = capture_date(photo.name)
            if captured is None or captured >= cutoff:
                continue
            video = photo.with_name(photo.name.removesuffix("_photo.jpg") + "_video.mp4")
            photo.unlink(missing_ok=True)
            video.unlink(missing_ok=True)
            removed += 1
    return removed


def all_sightings(root: Path, limit: int) -> list[str]:
    """Newest sorted sightings across all cameras. Runs in the executor."""
    base = root.resolve()
    found = []
    for photo in base.glob("*/*/*_photo.jpg"):
        relative = photo.relative_to(base)
        if relative.parts[1] in NON_SIGHTING_FOLDERS or relative.parts[0].startswith((".", "@")):
            continue
        found.append((sort_key(photo.name), relative.as_posix()))
    found.sort(reverse=True)
    return [relative for _, relative in found[:limit]]


def sort_key(name: str) -> str:
    """Sort by the Reveal capture time in the name (MMDDYYYYHHMMSS)."""
    match = re.search(r"-(\d{2})(\d{2})(\d{4})(\d{6})-", name)
    if match:
        month, day, year, clock = match.groups()
        return f"{year}{month}{day}{clock}"
    return name
