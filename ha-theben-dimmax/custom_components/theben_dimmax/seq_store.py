"""Persistent SEQ high-water-mark registry.


Bluetooth Mesh replay protection means a node permanently remembers the
highest SEQ it has ever accepted from each source address and silently
rejects anything at or below that -- forever, not just "this session",
and with no error sent back, so a rejected message looks identical to
one that was never sent at all.

CONF_SEQ living only in the config entry isn't enough to guarantee it
only ever moves forward, because two ordinary user actions can hand the
device a LOWER seq than it's already seen:

  1. Removing and re-adding the integration. The old entry (and whatever
     seq it had climbed to during live use) is gone; a fresh add starts
     from whatever CONF_SEQ the new setup computes, with no memory of
     the old entry ever having existed.

  2. Re-importing the app's export file. The export only ever contains a
     point-in-time SNAPSHOT of sequence numbers from whenever it was
     exported -- it has no idea what this integration has sent since.

Either one can silently regress SEQ backwards, at which point the device
drops every single message with zero indication why -- it just looks
like "stopped working" with no error anywhere. This store is keyed by
the dimmer's BLE MAC address specifically so it survives both of those
cases (a MAC address, unlike a config entry ID, doesn't change and isn't
deleted along with the integration).
"""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "theben_dimmax_seq_registry"


async def get_known_seq(hass: HomeAssistant, mac_address: str) -> int:
    """Return the highest SEQ ever recorded for this device's MAC, or 0
    if we've never seen this device before."""
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    return data.get(mac_address.upper(), 0)


async def record_seq(hass: HomeAssistant, mac_address: str, seq: int) -> None:
    """Record seq as the new high-water mark for this MAC, if it's
    actually higher than what's already stored. This must never move
    backwards -- that's the entire point of this file existing."""
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    mac = mac_address.upper()
    if seq > data.get(mac, 0):
        data[mac] = seq
        await store.async_save(data)
        _LOGGER.debug("SEQ registry: %s -> %d", mac, seq)
