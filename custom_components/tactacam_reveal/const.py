DOMAIN = "tactacam_reveal"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

API_BASE = "https://api.reveal.ishareit.net/v1"
AWS_REGION = "us-east-1"
USER_POOL_ID = "us-east-1_VdSxemNpw"
CLIENT_ID = "6r9tpojvgvkci5trla0ip14mon"
USER_AGENT = "RevealWeb/5.10.0"

UPDATE_INTERVAL_MINUTES = 5
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DOWNLOAD_PHOTOS = "download_photos"
CONF_DOWNLOAD_VIDEOS = "download_videos"
CONF_PERSISTENT_NOTIFICATIONS = "persistent_notifications"
EVENT_NEW_MEDIA = "tactacam_reveal_new_media"
EVENT_MEDIA_CLASSIFIED = "tactacam_reveal_media_classified"
STORAGE_VERSION = 1

# Classification options. A local camera-trap classifier server is tried first;
# the AI Task entity is the fallback when it is missing or not confident.
CONF_AUTO_CLASSIFY = "auto_classify"
CONF_CLASSIFIER_URL = "classifier_url"
CONF_CLASSIFIER_TOKEN = "classifier_token"
CONF_COUNTRY = "country"
CONF_ADMIN1_REGION = "admin1_region"
CONF_MIN_SCORE = "min_score"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_AI_INSTRUCTIONS = "ai_instructions"
CONF_QUIET_LABELS = "quiet_labels"
CONF_LABEL_ALIASES = "label_aliases"
DEFAULT_MIN_SCORE = 0.8
DEFAULT_QUIET_LABELS = "squirrel, vehicle"
DEFAULT_LABEL_ALIASES = (
    "eastern cottontail=rabbit, cottontail=rabbit, jackrabbit=rabbit, "
    "didelphis species=opossum, domestic dog=dog, domestic cat=cat"
)
NOTIFICATION_LINK_DAYS = 7
DEFAULT_BACKLOG_DAYS = 30

# A second, stronger AI Task entity used when the first AI answer is unsure.
CONF_AI_TASK_ENTITY_STRONG = "ai_task_entity_strong"
# Rough cloud price per AI call, used only for the savings estimate.
CONF_AI_COST_PER_CALL = "ai_cost_per_call"
DEFAULT_AI_COST_PER_CALL = 0.003

# Notifications sent by the integration itself.
CONF_NOTIFY_DEVICES = "notify_devices"
CONF_NOTIFY_COOLDOWN = "notify_cooldown_minutes"
CONF_ONLY_NOTIFY_LABELS = "only_notify_labels"
DEFAULT_NOTIFY_COOLDOWN = 15

# Per-camera overrides, stored as options["cameras"][<camera folder>].
CONF_CAMERAS = "cameras"
CONF_CAMERA_CLASSIFY = "classify"
CONF_CAMERA_NOTIFY = "notify"
CONF_SCENE_NOTES = "scene_notes"

# Delete photos classified as empty after this many days (0 keeps them).
CONF_FALSE_POSITIVE_RETENTION_DAYS = "false_positive_retention_days"

# Classifier health: raise a Repairs issue after this long offline.
CLASSIFIER_HEALTH_INTERVAL = 60
CLASSIFIER_OFFLINE_ISSUE_MINUTES = 10


def archive_location(hass):
    """Return the exposed media directory id and Reveal archive path."""
    media_dirs = hass.config.media_dirs
    if media_dirs:
        source_id = "local" if "local" in media_dirs else next(iter(media_dirs))
        from pathlib import Path

        return source_id, Path(media_dirs[source_id]) / "tactacam_reveal"
    from pathlib import Path

    return "local", Path(hass.config.path("media")) / "tactacam_reveal"


def locate_media(root, relative: str):
    """Find an archived file, following it if it was sorted into a subfolder.

    Returns None when the path escapes the archive or no file matches.
    """
    from pathlib import Path
    import re

    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    try:
        parts = candidate.relative_to(base).parts
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    if len(parts) < 2 or not re.fullmatch(r"[\w.-]+", parts[-1]):
        return None
    camera_dir = base / parts[0]
    if not camera_dir.is_dir():
        return None
    return next((p for p in camera_dir.rglob(parts[-1]) if p.is_file()), None)


# Videos are uploaded on request only. Reveal handles one request at a time, so
# requests are queued and the next is sent once the previous video arrived.
CONF_AUTO_REQUEST_VIDEOS = "auto_request_videos"
CONF_VIDEO_REQUEST_LABELS = "video_request_labels"
VIDEO_POLL_SECONDS = 60
VIDEO_REQUEST_TIMEOUT_MINUTES = 60
VIDEO_REQUEST_RETRIES = 3
VIDEO_QUEUE_LIMIT = 20
VIDEO_QUEUE_MAX_AGE_HOURS = 6
