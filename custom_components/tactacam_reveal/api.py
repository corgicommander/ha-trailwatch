from __future__ import annotations

import asyncio
from typing import Any
import os

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pycognito import Cognito
from botocore import UNSIGNED
from botocore.config import Config

from .const import API_BASE, AWS_REGION, CLIENT_ID, USER_AGENT, USER_POOL_ID


class RevealAuthenticationError(Exception):
    """Reveal rejected the supplied account credentials."""


class RevealApi:
    def __init__(self, hass: HomeAssistant, username: str, password: str) -> None:
        self._hass = hass
        self._username = username
        self._password = password
        self._cognito: Cognito | None = None
        # One login or token refresh at a time across concurrent requests.
        self._auth_lock = asyncio.Lock()

    def _authenticate_sync(self) -> None:
        # boto3 otherwise probes the EC2 metadata address while constructing
        # its client. Reveal authentication does not use instance credentials.
        os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
        cognito = Cognito(
            USER_POOL_ID,
            CLIENT_ID,
            username=self._username,
            user_pool_region=AWS_REGION,
            botocore_config=Config(signature_version=UNSIGNED),
        )
        cognito.authenticate(self._password)
        self._cognito = cognito

    async def async_authenticate(self) -> None:
        try:
            await self._hass.async_add_executor_job(self._authenticate_sync)
        except Exception as err:
            raise RevealAuthenticationError(str(err)) from err

    async def _async_token(self) -> str:
        async with self._auth_lock:
            if self._cognito is None or not self._cognito.access_token:
                await self.async_authenticate()
            else:
                try:
                    await self._hass.async_add_executor_job(self._cognito.check_token)
                except Exception:
                    await self.async_authenticate()
            assert self._cognito is not None
            return self._cognito.access_token

    async def async_get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        return await self._async_request("GET", path, params=params)

    async def _async_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict:
        token = await self._async_token()
        session = async_get_clientsession(self._hass)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Reveal-User-Agent": USER_AGENT,
        }
        url = f"{API_BASE}/{path.lstrip('/')}"
        async with session.request(
            method, url, headers=headers, params=params, json=body
        ) as response:
            if response.status == 401:
                async with self._auth_lock:
                    await self.async_authenticate()
                assert self._cognito is not None
                headers["Authorization"] = f"Bearer {self._cognito.access_token}"
                async with session.request(
                    method, url, headers=headers, params=params, json=body
                ) as retry:
                    retry.raise_for_status()
                    return await retry.json()
            response.raise_for_status()
            return await response.json()

    async def async_cameras(self) -> list[dict]:
        payload = await self.async_get("cameras", {"size": 100, "page": 0})
        return payload.get("response", {}).get("cameras", [])

    async def async_latest_photo(self, camera_id: str) -> dict | None:
        payload = await self.async_get(
            "photos/v2", {"size": 1, "cameraIds": camera_id}
        )
        photos = payload.get("response", {}).get("photos", [])
        return photos[0] if photos else None

    async def async_recent_media(self, camera_id: str, size: int = 100) -> list[dict]:
        payload = await self.async_get(
            "photos/v2", {"size": size, "cameraIds": camera_id}
        )
        return payload.get("response", {}).get("photos", [])

    async def async_videos(self, page: int = 0, size: int = 50) -> list[dict]:
        """Uploaded videos, newest first. Reveal only uploads requested videos."""
        payload = await self.async_get("videos", {"page": page, "size": size})
        return payload.get("response", {}).get("videos", [])

    async def async_request_video(self, photo_id: str) -> int:
        """Ask the camera to upload the video behind a photo; return how many
        requests Reveal refused (0 means accepted)."""
        payload = await self._async_request(
            "POST", "photos/batchVideoRequest", body={"photoIds": [photo_id]}
        )
        return int((payload.get("response") or {}).get("videosRefused") or 0)

    async def async_image(self, url: str) -> bytes:
        session = async_get_clientsession(self._hass)
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()
