"""The Theben DIMAX BLE Mesh integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_NETKEY, CONF_NETKEY_INDEX, CONF_PROVISIONER_ADDR, CONF_IV_INDEX
from .coordinator import ThebenDIMAXCoordinator
from .mesh_gateway import MeshGateway
from .theben_api import ThebenDIMAXAPI

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Theben DIMAX from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    config = entry.data

    # One MeshGateway shared by EVERY dimmer in the same mesh network --
    # see mesh_gateway.py's module docstring for why one BLE connection
    # can serve all of them instead of needing one connection per dimmer.
    # Created once, by whichever entry sets up first; every entry after
    # that registers itself with the same gateway instance.
    gateway: MeshGateway | None = hass.data[DOMAIN].get("gateway")
    if gateway is None:
        gateway = MeshGateway(
            hass,
            netkey=bytes.fromhex(config[CONF_NETKEY]),
            netkey_index=config.get(CONF_NETKEY_INDEX, 0),
            provisioner_addr=config[CONF_PROVISIONER_ADDR],
            iv_index=config[CONF_IV_INDEX],
        )
        hass.data[DOMAIN]["gateway"] = gateway
    else:
        # Sanity check -- this shared-gateway design assumes every entry
        # belongs to the same single mesh network (same export). A
        # different NetKey here means a genuinely different mesh, which
        # isn't supported by one shared connection.
        if bytes.fromhex(config[CONF_NETKEY]) != gateway.netkey:
            _LOGGER.error(
                "This entry's NetKey doesn't match the mesh network the "
                "shared gateway was created for. Only one mesh network "
                "per Home Assistant instance is supported by this "
                "integration -- if you have devices from two separate "
                "Theben app projects, they can't currently share a "
                "gateway."
            )

    api = ThebenDIMAXAPI(hass, entry, gateway)
    await api.async_initialize_seq()
    api.register_with_gateway()

    channels = [0]

    coordinator = ThebenDIMAXCoordinator(hass, api, channels)
    api.set_coordinator(coordinator)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning("Initial connection failed (will retry on commands): %s", err)

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, ["light", "sensor"])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["light", "sensor"])

    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        gateway: MeshGateway | None = hass.data[DOMAIN].get("gateway")
        if gateway is not None:
            coordinator.api.unregister_from_gateway()
            # Only the entry_id keys are per-dimmer; "gateway" itself
            # doesn't count as one. If nothing else is left, this was the
            # last dimmer using the gateway -- tear the shared connection
            # down rather than leaving it open for nobody.
            remaining = [k for k in hass.data[DOMAIN] if k != "gateway"]
            if not remaining:
                await gateway.disconnect()
                hass.data[DOMAIN].pop("gateway", None)

    return unload_ok
