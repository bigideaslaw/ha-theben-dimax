"""Light platform for Theben DimMax."""

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ADDRESS, DOMAIN
from .coordinator import ThebenDimMaxCoordinator

_LOGGER = logging.getLogger(__name__)

# How long to wait after commanding a Level/OnOff change before polling the
# device for its actual state. The dimmer physically transitions over some
# time and BLE round-trips aren't instant, so an immediate refresh right
# after sending a command tends to read a stale/in-flight value. 4s gives
# the transition time to settle before we ask.
POST_COMMAND_REFRESH_DELAY = 4


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build the HA device this dimmer's entities all belong under."""
    mac = entry.data.get(CONF_DEVICE_ADDRESS)
    info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or "Theben DimMax",
        manufacturer="Theben",
        model="DimMax 540 Series",
    )
    if mac:
        info["connections"] = {("bluetooth", mac)}
    return info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Theben DimMax lights from a config entry."""
    coordinator: ThebenDimMaxCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    single_channel = len(coordinator.channels) == 1
    for channel in coordinator.channels:
        entities.append(
            ThebenDimMaxLight(coordinator, channel, entry, single_channel)
        )

    async_add_entities(entities)


class ThebenDimMaxLight(CoordinatorEntity, LightEntity):
    """Representation of a Theben DimMax dimmer channel."""

    def __init__(
        self,
        coordinator: ThebenDimMaxCoordinator,
        channel: int,
        entry: ConfigEntry,
        single_channel: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}"
        self._attr_has_entity_name = True
        # Single-channel dimmer -> entity takes the device's own name
        # (shows up in HA as just "<Device Name>"). Multi-channel -> each
        # light gets its own name under the shared device.
        self._attr_name = None if single_channel else f"Channel {channel + 1}"
        self._attr_device_info = _device_info(entry)
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_color_mode = ColorMode.BRIGHTNESS

    @property
    def is_on(self) -> bool:
        """Return true if light is on."""
        data = self.coordinator.data
        if data and self._channel in data:
            return data[self._channel].get("on", False)
        return False

    @property
    def brightness(self) -> int | None:
        """Return brightness 0-255."""
        data = self.coordinator.data
        if data and self._channel in data:
            return data[self._channel].get("brightness", 0)
        return None

    async def _delayed_refresh(self) -> None:
        """Wait for the physical transition to settle, then poll real state."""
        await asyncio.sleep(POST_COMMAND_REFRESH_DELAY)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on.

        If HA passed an explicit brightness (slider drag), send it.
        Otherwise (the on/off button -- tapping the entity, an automation
        calling light.turn_on with no brightness, etc.) send only the
        OnOff Set and let the device resume whatever Level it last had --
        it already retains that itself. See turn_on() in theben_api.py.
        """
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        await self.coordinator.api.turn_on(
            brightness=brightness, channel=self._channel
        )
        # Don't block the service call for 4s -- schedule the settle-then-
        # poll as a background task so turn_on() returns immediately.
        self.hass.async_create_task(self._delayed_refresh())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.api.turn_off(channel=self._channel)
        self.hass.async_create_task(self._delayed_refresh())
