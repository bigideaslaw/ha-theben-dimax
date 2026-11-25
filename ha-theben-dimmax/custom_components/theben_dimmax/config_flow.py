"""Config flow for Theben DimMax BLE Mesh integration."""

import json
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN, CONF_NETKEY, CONF_APPKEY, CONF_PROVISIONER_ADDR,
    CONF_DIMMER_ADDR, CONF_IV_INDEX, CONF_NETKEY_INDEX,
    CONF_APPKEY_INDEX, CONF_SEQ, CONF_DEVICE_ADDRESS, CONF_TTL,
)
from .theben_api import parse_theben_export

_LOGGER = logging.getLogger(__name__)


class ThebenDimMaxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Theben DimMax."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Step 1: Choose manual entry or file import."""
        if user_input is not None:
            if user_input["method"] == "import":
                return await self.async_step_import()
            else:
                return await self.async_step_manual()

        data_schema = vol.Schema({
            vol.Required("method"): vol.In({
                "import": "Import from Theben app project export (JSON)",
                "manual": "Enter mesh configuration manually",
            }),
        })

        return self.async_show_form(step_id="user", data_schema=data_schema)

    async def async_step_manual(self, user_input=None) -> FlowResult:
        """Manual mesh configuration entry."""
        errors = {}

        if user_input is not None:
            try:
                config = self._validate_manual_config(user_input)
                await self.async_set_unique_id(
                    f"theben_dimmax_{config[CONF_DIMMER_ADDR]}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Theben DimMax (0x{config[CONF_DIMMER_ADDR]:04X})",
                    data=config,
                )
            except (ValueError, KeyError) as err:
                _LOGGER.error("Config validation error: %s", err)
                errors["base"] = "invalid_config"

        data_schema = vol.Schema({
            vol.Required(CONF_DEVICE_ADDRESS): str,
            vol.Required(CONF_NETKEY): str,
            vol.Required(CONF_APPKEY): str,
            vol.Required(CONF_PROVISIONER_ADDR, default=1): int,
            vol.Required(CONF_DIMMER_ADDR, default=2): int,
            vol.Required(CONF_IV_INDEX, default=0): int,
            vol.Optional(CONF_NETKEY_INDEX, default=0): int,
            vol.Optional(CONF_APPKEY_INDEX, default=0): int,
            vol.Optional(CONF_SEQ, default=800): int,
            vol.Optional(CONF_TTL, default=8): int,
        })

        return self.async_show_form(
            step_id="manual",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Import from Theben app project export JSON paste."""
        errors = {}

        if user_input is not None:
            try:
                raw_json = user_input["export_json"]
                export_data = json.loads(raw_json)
                devices = parse_theben_export(export_data)

                if not devices:
                    errors["base"] = "no_devices"
                elif len(devices) == 1:
                    config = devices[0]
                    await self.async_set_unique_id(
                        f"theben_dimmax_{config[CONF_DIMMER_ADDR]}"
                    )
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=f"Theben DimMax ({config.get('node_name', 'Unknown')})",
                        data=config,
                    )
                else:
                    self._devices = devices
                    return await self.async_step_select_device()

            except (json.JSONDecodeError, ValueError, KeyError) as err:
                _LOGGER.error("Import error: %s", err)
                errors["base"] = "invalid_export"

        data_schema = vol.Schema({
            vol.Required("export_json"): str,
        })

        return self.async_show_form(
            step_id="import",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": "Paste the entire JSON content from the Theben app project export file"
            },
        )

    async def async_step_select_device(self, user_input=None) -> FlowResult:
        """Select which device to add (when export has multiple)."""
        if user_input is not None:
            idx = int(user_input["device"])
            config = self._devices[idx]

            await self.async_set_unique_id(
                f"theben_dimmax_{config[CONF_DIMMER_ADDR]}"
            )
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Theben DimMax ({config.get('node_name', 'Unknown')})",
                data=config,
            )

        device_options = {
            str(i): f"{d.get('node_name', '?')} (0x{d[CONF_DIMMER_ADDR]:04X}) at {d[CONF_DEVICE_ADDRESS]}"
            for i, d in enumerate(self._devices)
        }

        data_schema = vol.Schema({
            vol.Required("device"): vol.In(device_options),
        })

        return self.async_show_form(step_id="select_device", data_schema=data_schema)

    def _validate_manual_config(self, user_input: dict) -> dict:
        """Validate manual configuration entry."""
        netkey = user_input[CONF_NETKEY].replace(":", "").replace(" ", "")
        if len(netkey) != 32:
            raise ValueError(f"NetKey must be 32 hex chars, got {len(netkey)}")
        bytes.fromhex(netkey)

        appkey = user_input[CONF_APPKEY].replace(":", "").replace(" ", "")
        if len(appkey) != 32:
            raise ValueError(f"AppKey must be 32 hex chars, got {len(appkey)}")
        bytes.fromhex(appkey)

        return {
            CONF_DEVICE_ADDRESS: user_input[CONF_DEVICE_ADDRESS].upper(),
            CONF_NETKEY: netkey,
            CONF_APPKEY: appkey,
            CONF_PROVISIONER_ADDR: user_input[CONF_PROVISIONER_ADDR],
            CONF_DIMMER_ADDR: user_input[CONF_DIMMER_ADDR],
            CONF_IV_INDEX: user_input[CONF_IV_INDEX],
            CONF_NETKEY_INDEX: user_input.get(CONF_NETKEY_INDEX, 0),
            CONF_APPKEY_INDEX: user_input.get(CONF_APPKEY_INDEX, 0),
            CONF_SEQ: user_input.get(CONF_SEQ, 800),
            CONF_TTL: user_input.get(CONF_TTL, 8),
        }
