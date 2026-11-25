"""Sensor platform for Theben DimMax -- power, voltage, current, temperature.

Power is derived from the dimmer's real measured line voltage and output
current (Power(W) = LineVoltage(V) * OutputCurrent(mA) / 1000, the same
formula used by the Theben app's own "Energy Monitor" screen) -- this is
not an estimate, it's the same calculation the vendor app does with the
same underlying hardware measurements.
"""

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ADDRESS, DOMAIN
from .coordinator import ThebenDimMaxCoordinator

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Same device the light entity registers -- keeps everything grouped
    under one device per dimmer instead of each platform inventing its own."""
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
    """Set up Theben DimMax sensors from a config entry."""
    coordinator: ThebenDimMaxCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    single_channel = len(coordinator.channels) == 1
    for channel in coordinator.channels:
        entities.append(ThebenDimMaxPowerSensor(coordinator, channel, entry, single_channel))
        entities.append(ThebenDimMaxVoltageSensor(coordinator, channel, entry, single_channel))
        entities.append(ThebenDimMaxCurrentSensor(coordinator, channel, entry, single_channel))
        entities.append(ThebenDimMaxTemperatureSensor(coordinator, channel, entry, single_channel))
        entities.append(ThebenDimMaxMinDimLevelSensor(coordinator, channel, entry, single_channel))

    async_add_entities(entities)


class _ThebenDimMaxSensorBase(CoordinatorEntity, SensorEntity):
    """Shared base for all DimMax sensors."""

    def __init__(self, coordinator, channel: int, entry: ConfigEntry, key: str,
                 name: str, single_channel: bool = True):
        super().__init__(coordinator)
        self._channel = channel
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_{key}"
        self._attr_has_entity_name = True
        self._attr_name = name if single_channel else f"Channel {channel + 1} {name}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        data = self.coordinator.data
        if data and self._channel in data:
            return data[self._channel].get(self._key)
        return None


class ThebenDimMaxPowerSensor(_ThebenDimMaxSensorBase):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, channel, entry, single_channel=True):
        super().__init__(coordinator, channel, entry, "power", "Power", single_channel)


class ThebenDimMaxVoltageSensor(_ThebenDimMaxSensorBase):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False  # secondary diagnostic value

    def __init__(self, coordinator, channel, entry, single_channel=True):
        super().__init__(coordinator, channel, entry, "line_voltage", "Line Voltage", single_channel)


class ThebenDimMaxCurrentSensor(_ThebenDimMaxSensorBase):
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.MILLIAMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False  # secondary diagnostic value

    def __init__(self, coordinator, channel, entry, single_channel=True):
        super().__init__(coordinator, channel, entry, "output_current_ma", "Output Current", single_channel)


class ThebenDimMaxTemperatureSensor(_ThebenDimMaxSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False  # unverified scaling, see mesh_messages.py

    def __init__(self, coordinator, channel, entry, single_channel=True):
        super().__init__(coordinator, channel, entry, "temperature", "Temperature", single_channel)


class ThebenDimMaxMinDimLevelSensor(_ThebenDimMaxSensorBase):
    """Diagnostic: the device's own configured minimum dim level (%).
    Enable this once to see the value, then it can be disabled again --
    it's here to help diagnose why commanded brightness doesn't match
    visible output, not for day-to-day use."""
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True  # enabled by default for this diagnosis

    def __init__(self, coordinator, channel, entry, single_channel=True):
        super().__init__(coordinator, channel, entry, "min_dim_level_pct", "Min Dim Level", single_channel)
