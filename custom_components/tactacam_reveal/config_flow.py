from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from aiohttp import ClientTimeout

from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RevealApi, RevealAuthenticationError
from .coordinator import _safe_name
from .const import DOMAIN
from .const import (
    CONF_ADMIN1_REGION,
    CONF_AI_COST_PER_CALL,
    CONF_AI_TASK_ENTITY_STRONG,
    CONF_AUTO_REQUEST_VIDEOS,
    CONF_VIDEO_REQUEST_LABELS,
    CONF_CAMERA_CLASSIFY,
    CONF_CAMERA_NOTIFY,
    CONF_CAMERAS,
    CONF_FALSE_POSITIVE_RETENTION_DAYS,
    CONF_NOTIFY_COOLDOWN,
    CONF_NOTIFY_DEVICES,
    CONF_ONLY_NOTIFY_LABELS,
    CONF_SCENE_NOTES,
    DEFAULT_AI_COST_PER_CALL,
    DEFAULT_NOTIFY_COOLDOWN,
    CONF_AI_INSTRUCTIONS,
    CONF_AI_TASK_ENTITY,
    CONF_AUTO_CLASSIFY,
    CONF_CLASSIFIER_TOKEN,
    CONF_CLASSIFIER_URL,
    CONF_COUNTRY,
    CONF_LABEL_ALIASES,
    CONF_MIN_SCORE,
    CONF_QUIET_LABELS,
    DEFAULT_LABEL_ALIASES,
    DEFAULT_MIN_SCORE,
    DEFAULT_QUIET_LABELS,
    CONF_DOWNLOAD_PHOTOS,
    CONF_DOWNLOAD_VIDEOS,
    CONF_PERSISTENT_NOTIFICATIONS,
    CONF_SCAN_INTERVAL,
    UPDATE_INTERVAL_MINUTES,
)


class TactacamRevealConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            api = RevealApi(
                self.hass,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await api.async_authenticate()
                await api.async_cameras()
            except RevealAuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Tactacam Reveal",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                        CONF_DOWNLOAD_PHOTOS: user_input[CONF_DOWNLOAD_PHOTOS],
                        CONF_DOWNLOAD_VIDEOS: user_input[CONF_DOWNLOAD_VIDEOS],
                        CONF_PERSISTENT_NOTIFICATIONS: user_input[
                            CONF_PERSISTENT_NOTIFICATIONS
                        ],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=UPDATE_INTERVAL_MINUTES
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                    vol.Required(CONF_DOWNLOAD_PHOTOS, default=True): bool,
                    vol.Required(CONF_DOWNLOAD_VIDEOS, default=True): bool,
                    vol.Required(CONF_PERSISTENT_NOTIFICATIONS, default=False): bool,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return TactacamRevealOptionsFlow(config_entry)


def _optional(key: str, current: dict) -> vol.Optional:
    """An optional field that shows the current value but can be cleared."""
    return vol.Optional(key, description={"suggested_value": current.get(key)})


TEXT = selector.TextSelector()
MULTILINE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def _number(minimum, maximum, step=1, unit=None, mode=selector.NumberSelectorMode.BOX):
    config = {"min": minimum, "max": maximum, "step": step, "mode": mode}
    if unit:  # unit_of_measurement must be a string when present
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


class TactacamRevealOptionsFlow(config_entries.OptionsFlow):
    """Options split into a short menu instead of one long form."""

    def __init__(self, config_entry):
        self._entry = config_entry
        self._options = {**config_entry.data, **config_entry.options}
        self._options.pop(CONF_USERNAME, None)
        self._options.pop(CONF_PASSWORD, None)
        self._cameras: list[str] = []

    def _save(self, user_input: dict, optional: tuple[str, ...] = ()):
        # Optional fields left empty are omitted from user_input: clear them.
        for key in optional:
            self._options.pop(key, None)
        self._options.update(user_input)
        return self.async_create_entry(title="", data=self._options)

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["downloads", "classification", "notifications", "cameras"],
        )

    async def async_step_downloads(self, user_input=None):
        if user_input is not None:
            if user_input.get(CONF_AUTO_REQUEST_VIDEOS):
                # Ask which animals trigger a request on a second page.
                self._options.update(user_input)
                return await self.async_step_video_requests()
            return self._save(user_input)
        current = self._options
        return self.async_show_form(
            step_id="downloads",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current.get(CONF_SCAN_INTERVAL, UPDATE_INTERVAL_MINUTES),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                    vol.Required(
                        CONF_DOWNLOAD_PHOTOS, default=current.get(CONF_DOWNLOAD_PHOTOS, True)
                    ): bool,
                    vol.Required(
                        CONF_DOWNLOAD_VIDEOS, default=current.get(CONF_DOWNLOAD_VIDEOS, True)
                    ): bool,
                    vol.Required(
                        CONF_AUTO_REQUEST_VIDEOS,
                        default=current.get(CONF_AUTO_REQUEST_VIDEOS, False),
                    ): bool,
                    vol.Required(
                        CONF_PERSISTENT_NOTIFICATIONS,
                        default=current.get(CONF_PERSISTENT_NOTIFICATIONS, False),
                    ): bool,
                    vol.Required(
                        CONF_FALSE_POSITIVE_RETENTION_DAYS,
                        default=current.get(CONF_FALSE_POSITIVE_RETENTION_DAYS, 0),
                    ): _number(0, 3650, unit="days"),
                }
            ),
        )

    async def async_step_video_requests(self, user_input=None):
        """Shown after Downloads & storage when automatic requests are on."""
        if user_input is not None:
            # A blank field is left out of user_input: _save clears the old
            # list, which means "request every video".
            return self._save(user_input, (CONF_VIDEO_REQUEST_LABELS,))
        return self.async_show_form(
            step_id="video_requests",
            data_schema=vol.Schema(
                {_optional(CONF_VIDEO_REQUEST_LABELS, self._options): TEXT}
            ),
        )

    async def async_step_classification(self, user_input=None):
        errors = {}
        if user_input is not None:
            errors = await self._validate_classification(user_input)
            if not errors:
                return self._save(
                    user_input,
                    (
                        CONF_CLASSIFIER_URL,
                        CONF_CLASSIFIER_TOKEN,
                        CONF_COUNTRY,
                        CONF_ADMIN1_REGION,
                        CONF_AI_TASK_ENTITY,
                        CONF_AI_TASK_ENTITY_STRONG,
                        CONF_AI_INSTRUCTIONS,
                    ),
                )
        current = {**self._options, **(user_input or {})}
        ai_entity = selector.EntitySelector(selector.EntitySelectorConfig(domain="ai_task"))
        return self.async_show_form(
            step_id="classification",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AUTO_CLASSIFY, default=current.get(CONF_AUTO_CLASSIFY, True)
                    ): bool,
                    _optional(CONF_CLASSIFIER_URL, current): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    _optional(CONF_CLASSIFIER_TOKEN, current): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    _optional(CONF_COUNTRY, current): TEXT,
                    _optional(CONF_ADMIN1_REGION, current): TEXT,
                    vol.Required(
                        CONF_MIN_SCORE, default=current.get(CONF_MIN_SCORE, DEFAULT_MIN_SCORE)
                    ): _number(0.5, 0.99, 0.01, mode=selector.NumberSelectorMode.SLIDER),
                    _optional(CONF_AI_TASK_ENTITY, current): ai_entity,
                    _optional(CONF_AI_TASK_ENTITY_STRONG, current): ai_entity,
                    _optional(CONF_AI_INSTRUCTIONS, current): MULTILINE,
                    vol.Required(
                        CONF_LABEL_ALIASES,
                        default=current.get(CONF_LABEL_ALIASES, DEFAULT_LABEL_ALIASES),
                    ): TEXT,
                    vol.Required(
                        CONF_AI_COST_PER_CALL,
                        default=current.get(CONF_AI_COST_PER_CALL, DEFAULT_AI_COST_PER_CALL),
                    ): _number(0, 1, 0.001, unit="USD"),
                }
            ),
        )

    async def _validate_classification(self, user_input: dict) -> dict:
        errors = {}
        url = (user_input.get(CONF_CLASSIFIER_URL) or "").rstrip("/")
        if url:
            token = user_input.get(CONF_CLASSIFIER_TOKEN)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                async with async_get_clientsession(self.hass).get(
                    f"{url}/health", headers=headers, timeout=ClientTimeout(total=10)
                ) as response:
                    response.raise_for_status()
            except Exception:
                errors[CONF_CLASSIFIER_URL] = "cannot_reach_classifier"
        for key in (CONF_AI_TASK_ENTITY, CONF_AI_TASK_ENTITY_STRONG):
            entity_id = user_input.get(key)
            state = entity_id and self.hass.states.get(entity_id)
            # AI Task features: 1 = generate data, 2 = attachments.
            if entity_id and (not state or (state.attributes.get("supported_features", 0) & 3) != 3):
                errors[key] = "ai_no_attachments"
        return errors

    async def async_step_notifications(self, user_input=None):
        if user_input is not None:
            return self._save(user_input, (CONF_NOTIFY_DEVICES, CONF_ONLY_NOTIFY_LABELS))
        current = self._options
        return self.async_show_form(
            step_id="notifications",
            data_schema=vol.Schema(
                {
                    _optional(CONF_NOTIFY_DEVICES, current): selector.DeviceSelector(
                        selector.DeviceSelectorConfig(integration="mobile_app", multiple=True)
                    ),
                    vol.Required(
                        CONF_NOTIFY_COOLDOWN,
                        default=current.get(CONF_NOTIFY_COOLDOWN, DEFAULT_NOTIFY_COOLDOWN),
                    ): _number(0, 240, unit="min"),
                    vol.Required(
                        CONF_QUIET_LABELS,
                        default=current.get(CONF_QUIET_LABELS, DEFAULT_QUIET_LABELS),
                    ): TEXT,
                    _optional(CONF_ONLY_NOTIFY_LABELS, current): TEXT,
                }
            ),
        )

    async def async_step_cameras(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._cameras = list(user_input.get("cameras") or [])
            if self._cameras:
                return await self.async_step_camera_settings()
            errors["base"] = "no_camera_selected"
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        folders = sorted(
            {
                _safe_name(camera["camera"].get("name") or camera_id)
                for camera_id, camera in ((coordinator.data or {}) if coordinator else {}).items()
            }
        )
        customized = sorted((self._options.get(CONF_CAMERAS) or {}).keys())
        return self.async_show_form(
            step_id="cameras",
            errors=errors,
            description_placeholders={
                "customized": ", ".join(c.replace("_", " ") for c in customized) or "none"
            },
            data_schema=vol.Schema(
                {
                    vol.Required("cameras", default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=f, label=f.replace("_", " "))
                                for f in folders
                            ],
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_camera_settings(self, user_input=None):
        cameras = dict(self._options.get(CONF_CAMERAS) or {})
        # Pre-fill from the first selected camera.
        current = dict(cameras.get(self._cameras[0]) or {})
        if user_input is not None:
            settings = {k: v for k, v in user_input.items() if v not in (None, "")}
            reset = settings.pop("use_global", False)
            for camera in self._cameras:
                if reset:
                    cameras.pop(camera, None)
                else:
                    cameras[camera] = dict(settings)
            return self._save({CONF_CAMERAS: cameras})
        return self.async_show_form(
            step_id="camera_settings",
            description_placeholders={
                "camera": ", ".join(c.replace("_", " ") for c in self._cameras)
            },
            data_schema=vol.Schema(
                {
                    vol.Required("use_global", default=False): bool,
                    vol.Required(
                        CONF_CAMERA_CLASSIFY, default=current.get(CONF_CAMERA_CLASSIFY, True)
                    ): bool,
                    vol.Required(
                        CONF_CAMERA_NOTIFY, default=current.get(CONF_CAMERA_NOTIFY, True)
                    ): bool,
                    _optional(CONF_ONLY_NOTIFY_LABELS, current): TEXT,
                    _optional(CONF_QUIET_LABELS, current): TEXT,
                    _optional(CONF_SCENE_NOTES, current): MULTILINE,
                }
            ),
        )
