"""Support for ADS sensors."""

from typing import override

import probatio
import pyads

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASSES_SCHEMA as SENSOR_DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    STATE_CLASSES_SCHEMA as SENSOR_STATE_CLASSES_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME, CONF_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, StateType

from . import ADS_TYPEMAP, CONF_ADS_FACTOR, CONF_ADS_TYPE
from .const import CONF_ADS_VAR, DATA_ADS, STATE_KEY_STATE, AdsType
from .entity import AdsEntity, AdsHubEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS sensor"

# The ADS device states, as the sensor reports them. Keyed by the value
# read_state() returns.
ADS_STATES = {
    pyads.ADSSTATE_INVALID: "invalid",
    pyads.ADSSTATE_IDLE: "idle",
    pyads.ADSSTATE_RESET: "reset",
    pyads.ADSSTATE_INIT: "init",
    pyads.ADSSTATE_START: "start",
    pyads.ADSSTATE_RUN: "run",
    pyads.ADSSTATE_STOP: "stop",
    pyads.ADSSTATE_SAVECFG: "savecfg",
    pyads.ADSSTATE_LOADCFG: "loadcfg",
    pyads.ADSSTATE_POWERFAILURE: "powerfailure",
    pyads.ADSSTATE_POWERGOOD: "powergood",
    pyads.ADSSTATE_ERROR: "error",
    pyads.ADSSTATE_SHUTDOWN: "shutdown",
    pyads.ADSSTATE_SUSPEND: "suspend",
    pyads.ADSSTATE_RESUME: "resume",
    pyads.ADSSTATE_CONFIG: "config",
    pyads.ADSSTATE_RECONFIG: "reconfig",
}

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_ADS_VAR): cv.string,
        probatio.Optional(CONF_ADS_FACTOR): cv.positive_int,
        probatio.Optional(CONF_ADS_TYPE, default=AdsType.INT): probatio.All(
            probatio.Coerce(AdsType),
            probatio.In(
                [
                    AdsType.BOOL,
                    AdsType.BYTE,
                    AdsType.INT,
                    AdsType.UINT,
                    AdsType.SINT,
                    AdsType.USINT,
                    AdsType.DINT,
                    AdsType.UDINT,
                    AdsType.WORD,
                    AdsType.DWORD,
                    AdsType.LREAL,
                    AdsType.REAL,
                ]
            ),
        ),
        probatio.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        probatio.Optional(CONF_DEVICE_CLASS): SENSOR_DEVICE_CLASSES_SCHEMA,
        probatio.Optional(CONF_STATE_CLASS): SENSOR_STATE_CLASSES_SCHEMA,
        probatio.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up an ADS sensor device."""
    ads_hub = hass.data[DATA_ADS]

    if discovery_info is not None:
        add_entities([AdsStateSensor(ads_hub)])
        return

    ads_var: str = config[CONF_ADS_VAR]
    ads_type: AdsType = config[CONF_ADS_TYPE]
    name: str = config[CONF_NAME]
    factor: int | None = config.get(CONF_ADS_FACTOR)
    device_class: SensorDeviceClass | None = config.get(CONF_DEVICE_CLASS)
    state_class: SensorStateClass | None = config.get(CONF_STATE_CLASS)
    unit_of_measurement: str | None = config.get(CONF_UNIT_OF_MEASUREMENT)

    entity = AdsSensor(
        ads_hub,
        ads_var,
        ads_type,
        name,
        factor,
        device_class,
        state_class,
        unit_of_measurement,
    )

    add_entities([entity])


class AdsSensor(AdsEntity, SensorEntity):
    """Representation of an ADS sensor entity."""

    def __init__(
        self,
        ads_hub: AdsHub,
        ads_var: str,
        ads_type: AdsType,
        name: str,
        factor: int | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
        unit_of_measurement: str | None,
    ) -> None:
        """Initialize AdsSensor entity."""
        super().__init__(ads_hub, name, ads_var)
        self._ads_type = ads_type
        self._factor = factor
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit_of_measurement

    @override
    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await super().async_added_to_hass()
        await self.async_initialize_device(
            self._ads_var,
            ADS_TYPEMAP[self._ads_type],
            STATE_KEY_STATE,
            self._factor,
        )

    @property
    @override
    def native_value(self) -> StateType:
        """Return the state of the device."""
        return self._state_dict[STATE_KEY_STATE]


class AdsStateSensor(AdsHubEntity, SensorEntity):
    """Representation of what the ADS device is doing."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ADS_STATES.values())

    def __init__(self, ads_hub: AdsHub) -> None:
        """Initialize the ADS state sensor."""
        super().__init__(ads_hub, "ads_state")

    @property
    @override
    def native_value(self) -> str | None:
        """Return the device's state, unknown while it is not answering."""
        if (ads_state := self._ads_hub.ads_state) is None:
            return None
        return ADS_STATES.get(ads_state)
