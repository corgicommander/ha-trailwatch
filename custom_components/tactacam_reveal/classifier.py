"""Classify archived photos: a local SpeciesNet server first, AI Task as fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .archive import (
    FALSE_POSITIVES,
    UNCERTAIN,
    camera_reference,
    category_folder,
)
from .const import (
    CONF_ADMIN1_REGION,
    CONF_AI_INSTRUCTIONS,
    CONF_AI_TASK_ENTITY,
    CONF_AI_TASK_ENTITY_STRONG,
    CONF_CAMERAS,
    CONF_CLASSIFIER_TOKEN,
    CONF_CLASSIFIER_URL,
    CONF_COUNTRY,
    CONF_LABEL_ALIASES,
    CONF_MIN_SCORE,
    CONF_SCENE_NOTES,
    DEFAULT_LABEL_ALIASES,
    DEFAULT_MIN_SCORE,
    DOMAIN,
    archive_location,
)

_LOGGER = logging.getLogger(__name__)

# A blank verdict is only trusted when no detection is even this likely.
BLANK_MAX_DETECTION = 0.2
HUMAN_MIN_DETECTION = 0.5

AI_INSTRUCTIONS = """\
The first image is from a motion-triggered trail camera named {camera}. Most \
triggers are false alarms from wind, sunlight or shadows, so be skeptical. Only \
report a person or animal when its body is clearly visible with recognizable \
features such as a head, eyes, legs, fur, feathers or shell pattern. Rocks, \
stumps, logs, wood chips, leaves, holes and shadows are not animals, and do not \
guess at animals that could be hidden in a hole or shadow.{reference}{scene}{hint}

Set category to the kinds of people and animals visible, as lowercase singular \
common names (for example human, dog, deer, turtle, squirrel, bird, raccoon), \
each kind once, joined by hyphens in alphabetical order, such as dog-human. Use \
human for any person. If no person or animal is visible, set category to \
falsepositives. Set confidence to high only when every subject is unmistakable, \
otherwise medium or low. An empty scene is high confidence falsepositives. Set \
subject to a short description and reason to one sentence explaining it.{extra}"""

AI_STRUCTURE = {
    "category": {
        "description": "Hyphen-joined subjects such as dog-human, or falsepositives.",
        "required": True,
        "selector": {"text": {}},
    },
    "confidence": {
        "description": "One of high, medium or low.",
        "required": True,
        "selector": {"text": {}},
    },
    "subject": {
        "description": "Short identification, such as person, deer, dog, or nothing.",
        "required": True,
        "selector": {"text": {}},
    },
    "reason": {
        "description": "One concise sentence explaining the decision.",
        "required": True,
        "selector": {"text": {}},
    },
}


@dataclass
class Classification:
    category: str
    labels: list[str] = field(default_factory=list)
    notify: bool = False
    subject: str = ""
    reason: str = ""
    source: str = "none"
    confidence: str = "low"
    local: dict[str, Any] | None = None
    ai_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    """Flatten an AI field that may come back wrapped, e.g. {"result": "deer"}."""
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    return "" if value is None else str(value)


def _parse_list(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _parse_aliases(value: str) -> dict[str, str]:
    aliases = {}
    for pair in value.split(","):
        if "=" in pair:
            name, alias = pair.split("=", 1)
            aliases[name.strip().lower()] = alias.strip().lower()
    return aliases


class MediaClassifier:
    """Decide which folder an archived photo belongs in and whether to notify."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        self.hass = hass
        self.url = (options.get(CONF_CLASSIFIER_URL) or "").rstrip("/")
        self.token = options.get(CONF_CLASSIFIER_TOKEN) or ""
        self.country = options.get(CONF_COUNTRY) or hass.config.country or ""
        self.admin1 = options.get(CONF_ADMIN1_REGION) or ""
        self.min_score = float(options.get(CONF_MIN_SCORE, DEFAULT_MIN_SCORE))
        self.ai_entity = options.get(CONF_AI_TASK_ENTITY) or ""
        self.ai_entity_strong = options.get(CONF_AI_TASK_ENTITY_STRONG) or ""
        self.ai_extra = options.get(CONF_AI_INSTRUCTIONS) or ""
        self.scene_notes = {
            folder: (camera.get(CONF_SCENE_NOTES) or "").strip()
            for folder, camera in (options.get(CONF_CAMERAS) or {}).items()
        }
        # Called with (ok, error message) after each AI Task attempt.
        self.on_ai_result: Callable[[bool, str | None], None] | None = None
        self.aliases = _parse_aliases(
            options.get(CONF_LABEL_ALIASES, DEFAULT_LABEL_ALIASES)
        )
        # Keep load on the local model and the AI provider modest.
        self._semaphore = asyncio.Semaphore(2)

    @property
    def enabled(self) -> bool:
        return bool(self.url or self.ai_entity)

    async def async_classify(self, relative: str) -> Classification:
        """Classify a photo. `notify` here only means "this is a sighting";
        per-camera rules and cooldowns are applied by the coordinator."""
        async with self._semaphore:
            result = await self._async_classify(relative)
        result.notify = result.category not in (FALSE_POSITIVES, UNCERTAIN)
        return result

    async def _async_classify(self, relative: str) -> Classification:
        local = None
        if self.url:
            try:
                local = await self._async_local(relative)
            except (ClientError, TimeoutError, OSError) as err:
                _LOGGER.warning("Local classifier unavailable for %s: %s", relative, err)
            if local is not None:
                decided = self._decide_local(local)
                if decided is not None:
                    return decided
        if self.ai_entity:
            result = await self._async_ai_tracked(self.ai_entity, relative, local)
            if (
                result.category == UNCERTAIN
                and self.ai_entity_strong
                and self.ai_entity_strong != self.ai_entity
            ):
                stronger = await self._async_ai_tracked(
                    self.ai_entity_strong, relative, local
                )
                stronger.ai_calls += result.ai_calls
                return stronger
            return result
        return Classification(
            category=UNCERTAIN,
            source="local" if local else "none",
            local=local,
            subject=(local or {}).get("common_name", ""),
            reason="The local classifier was not confident and no AI fallback is set.",
        )

    async def async_local_video(self, relative: str) -> dict[str, Any] | None:
        """Classify an archived video on the local server (frame sampling).

        Returns None when no server is set or it is unreachable. The result
        includes `frame_jpeg`, the deciding frame as base64.
        """
        if not self.url:
            return None
        try:
            async with self._semaphore:
                return await self._async_post(relative, "/v1/classify_video", "video/mp4", 300)
        except (ClientError, TimeoutError, OSError) as err:
            _LOGGER.warning("Local classifier unavailable for video %s: %s", relative, err)
            return None

    async def async_decide_video(
        self, local: dict[str, Any] | None, frame_relative: str | None
    ) -> Classification:
        """Decide a video from its server result; ask AI about the deciding
        frame (an archived JPEG) when the local model is not sure."""
        async with self._semaphore:
            result = None
            if local is not None:
                result = self._decide_local(local)
                if result is not None:
                    result.reason = result.reason.replace("Local model:", "Local model (video):", 1)
            if result is None and self.ai_entity and frame_relative:
                result = await self._async_ai_tracked(self.ai_entity, frame_relative, local)
                if (
                    result.category == UNCERTAIN
                    and self.ai_entity_strong
                    and self.ai_entity_strong != self.ai_entity
                ):
                    stronger = await self._async_ai_tracked(
                        self.ai_entity_strong, frame_relative, local
                    )
                    stronger.ai_calls += result.ai_calls
                    result = stronger
            if result is None:
                result = Classification(
                    category=UNCERTAIN,
                    source="local" if local else "none",
                    local=local,
                    subject=(local or {}).get("common_name", ""),
                    reason="The local classifier was not confident about the video.",
                )
        result.notify = result.category not in (FALSE_POSITIVES, UNCERTAIN)
        if result.local:
            result.local = {k: v for k, v in result.local.items() if k != "frame_jpeg"}
        return result

    async def async_ask_ai(self, relative: str, *, strong: bool = True) -> Classification:
        """One AI opinion on a photo, preferring the stronger entity."""
        entity = (self.ai_entity_strong if strong else "") or self.ai_entity
        if not entity:
            raise ValueError("No AI Task entity is set")
        async with self._semaphore:
            result = await self._async_ai_tracked(entity, relative, None)
        result.notify = result.category not in (FALSE_POSITIVES, UNCERTAIN)
        return result

    def label_for(self, local: dict[str, Any]) -> str | None:
        """Turn a SpeciesNet result into a short folder label, or None if too vague."""
        common = (local.get("common_name") or "").lower()
        if local.get("kind") == "unknown" or common in ("unknown", "no cv result"):
            return None
        if common in self.aliases:
            return self.aliases[common]
        taxonomy = local.get("taxonomy") or {}
        if common in ("human", "vehicle", "bird"):
            return common
        if not taxonomy.get("genus") or common.endswith(" species"):
            return None
        word = common.split()[-1]
        return self.aliases.get(word, word)

    def _decide_local(self, local: dict[str, Any]) -> Classification | None:
        score = local.get("score") or 0.0
        detections = local.get("detections") or []
        top_detection = max((d.get("confidence") or 0 for d in detections), default=0)
        human = max(
            (d.get("confidence") or 0 for d in detections if d.get("label") == "human"),
            default=0,
        )
        common = local.get("common_name", "")
        base = {"source": "local", "local": local, "confidence": f"{score:.2f}"}
        if local.get("kind") == "unknown":
            return None
        if local.get("kind") == "blank":
            if score >= self.min_score and top_detection < BLANK_MAX_DETECTION:
                return Classification(
                    category=FALSE_POSITIVES,
                    subject="nothing",
                    reason=f"Local model: blank ({score:.0%}).",
                    **base,
                )
            return None
        label = self.label_for(local)
        if label is None or score < self.min_score:
            return None
        labels = [label]
        if human >= HUMAN_MIN_DETECTION and "human" not in labels:
            labels.append("human")
        return Classification(
            category=category_folder("-".join(labels)),
            labels=sorted(labels),
            subject=common,
            reason=f"Local model: {common} ({score:.0%}).",
            **base,
        )

    async def _async_local(self, relative: str) -> dict[str, Any]:
        return await self._async_post(relative, "/v1/classify", "image/jpeg", 60)

    async def _async_post(
        self, relative: str, path: str, content_type: str, timeout: int
    ) -> dict[str, Any]:
        _, root = archive_location(self.hass)
        content = await self.hass.async_add_executor_job(
            (root / relative).resolve().read_bytes
        )
        params = {k: v for k, v in (("country", self.country), ("admin1_region", self.admin1)) if v}
        headers = {"Content-Type": content_type}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        session = async_get_clientsession(self.hass)
        async with session.post(
            f"{self.url}{path}",
            data=content,
            params=params,
            headers=headers,
            timeout=ClientTimeout(total=timeout),
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _async_ai_tracked(
        self, entity_id: str, relative: str, local: dict[str, Any] | None
    ) -> Classification:
        try:
            result = await self._async_ai(entity_id, relative, local)
        except Exception as err:
            if self.on_ai_result:
                self.on_ai_result(False, f"{entity_id}: {err}")
            raise
        if self.on_ai_result:
            self.on_ai_result(True, None)
        return result

    async def _async_ai(
        self, entity_id: str, relative: str, local: dict[str, Any] | None
    ) -> Classification:
        _, root = archive_location(self.hass)
        camera_folder = relative.split("/", 1)[0]
        references, scene_file = await self.hass.async_add_executor_job(
            camera_reference, root, camera_folder
        )
        scene = self.scene_notes.get(camera_folder) or scene_file
        hint = ""
        if local and local.get("kind") != "blank":
            hint = (
                f" A local wildlife model suggested '{local.get('common_name')}' with "
                f"low confidence ({(local.get('score') or 0):.0%}); verify it yourself."
            )
        instructions = AI_INSTRUCTIONS.format(
            camera=camera_folder.replace("_", " "),
            reference=(
                " The following image(s) are reference photos of the same scene with "
                "nothing in it. Anything that also appears in a reference is "
                "background; only report people or animals that are not in it."
                if references
                else ""
            ),
            scene=f" Scene notes: {scene}" if scene else "",
            hint=hint,
            extra=f"\n\n{self.ai_extra}" if self.ai_extra else "",
        )
        attachments = [
            {"media_content_id": f"media-source://{DOMAIN}/{path}", "media_content_type": "image/jpeg"}
            for path in [relative, *references]
        ]
        response = await self.hass.services.async_call(
            "ai_task",
            "generate_data",
            {
                "entity_id": entity_id,
                "task_name": "Classify trail camera photo",
                "instructions": instructions,
                "structure": AI_STRUCTURE,
                "attachments": attachments,
            },
            blocking=True,
            return_response=True,
        )
        data = (response or {}).get("data") or {}
        _LOGGER.debug("AI Task result for %s: %r", relative, data)
        if not isinstance(data, dict):
            data = {}
        confidence = _text(data.get("confidence", "low")).strip().lower()
        category = category_folder(_text(data.get("category")))
        if category != FALSE_POSITIVES and confidence != "high":
            category = UNCERTAIN
        labels = [] if category in (FALSE_POSITIVES, UNCERTAIN) else category.split("-")
        return Classification(
            category=category,
            labels=labels,
            subject=_text(data.get("subject")),
            reason=_text(data.get("reason")),
            source="ai",
            confidence=confidence,
            local=local,
            ai_calls=1,
        )
