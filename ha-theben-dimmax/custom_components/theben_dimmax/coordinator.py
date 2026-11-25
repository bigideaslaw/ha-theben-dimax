"""DataUpdateCoordinator for Theben DimMax."""

import logging
import random
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .theben_api import ThebenDimMaxAPI
from .const import DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


class ThebenDimMaxCoordinator(DataUpdateCoordinator):
    """Coordinator to poll Theben DimMax state via BLE Mesh."""

    def __init__(self, hass: HomeAssistant, api: ThebenDimMaxAPI, channels: list[int]):
        # All dimmers now share ONE connection via MeshGateway, so there's
        # no connect collision to avoid anymore -- but polls from several
        # dimmers still queue up back-to-back on the gateway if they all
        # start from the same moment (HA startup) on the same fixed
        # interval. A few seconds of random jitter, fixed once per
        # coordinator instead of re-rolled every cycle, spreads that out
        # instead of every dimmer's poll landing in the same instant every
        # single cycle, forever.
        jitter = random.uniform(-5, 5)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL + jitter),
        )
        self.api = api
        self.channels = channels

    async def _async_update_data(self):
        """Fetch data from the dimmer.

        No disconnect here, unlike previous versions -- every dimmer now
        shares ONE persistent connection through MeshGateway (see
        mesh_gateway.py), addressed by mesh destination rather than by
        opening its own BLE link. That's what removed the original
        reason to disconnect between polls (a single-radio adapter having
        only a handful of, sometimes just one, BLE connection slot to
        divide up across dimmers) -- there's only ever one connection
        now, regardless of how many dimmers are registered.
        """
        try:
            data = {}
            for ch in self.channels:
                await self.api.request_state(ch)
                data[ch] = await self.api.get_state(ch)
            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with DimMax: {err}") from err
