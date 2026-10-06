"""Shared device info for integration-level (hub) entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def hub_device_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Tactacam Reveal",
        manufacturer="Tactacam",
        model="Reveal account",
    )


def camera_device_info(camera_id: str) -> DeviceInfo:
    """Device of one Reveal camera (created by the camera platform)."""
    return DeviceInfo(identifiers={(DOMAIN, camera_id)})
