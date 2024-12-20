"""Support for ADS sensors."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASSES_SCHEMA as SENSOR_DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    STATE_CLASSES_SCHEMA as SENSOR_STATE_CLASSES_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME, CONF_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, StateType

from . import ADS_TYPEMAP, CONF_ADS_TYPE
from .const import CONF_ADS_FACTOR, CONF_ADS_VAR, DATA_ADS, STATE_KEY_STATE, AdsType
from .entity import AdsEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS sensor"

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_ADS_FACTOR): cv.positive_int,
        vol.Optional(CONF_ADS_TYPE, default=AdsType.INT): vol.All(
            vol.Coerce(AdsType),
            vol.In(
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
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): SENSOR_DEVICE_CLASSES_SCHEMA,
        vol.Optional(CONF_STATE_CLASS): SENSOR_STATE_CLASSES_SCHEMA,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
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

    ads_var: str = config[CONF_ADS_VAR]
    ads_type: AdsType = config[CONF_ADS_TYPE]
    name: str = config[CONF_NAME]
    factor: int | None = config.get(CONF_ADS_FACTOR)
    device_class: SensorDeviceClass | None = config.get(CONF_DEVICE_CLASS)
    state_class: SensorStateClass | None = config.get(CONF_STATE_CLASS)
    unit_of_measurement: str | None = config.get(CONF_UNIT_OF_MEASUREMENT)
    unique_id: str = f"{ads_hub.get_mac_address()}_{ads_var}"

    entity = AdsSensor(
        ads_hub,
        ads_var,
        ads_type,
        name,
        factor,
        device_class,
        state_class,
        unit_of_measurement,
        unique_id,
    )

    add_entities([entity])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ADS sensors from a config entry."""
    ads_hub = hass.data[DATA_ADS]
    sensors = entry.options.get("sensors", [])
    entities = []

    for sensor_config in sensors:
        ads_var: str = sensor_config[CONF_ADS_VAR]
        ads_type: AdsType = sensor_config[CONF_ADS_TYPE]
        name: str = sensor_config[CONF_NAME]
        factor: int | None = sensor_config.get(CONF_ADS_FACTOR)
        device_class: SensorDeviceClass | None = sensor_config.get(CONF_DEVICE_CLASS)
        state_class: SensorStateClass | None = sensor_config.get(CONF_STATE_CLASS)
        unit_of_measurement: str | None = sensor_config.get(CONF_UNIT_OF_MEASUREMENT)
        # Generate a unique ID for each sensor
        unique_id: str = f"{ads_hub.get_mac_address()}_{ads_var}"

        entities.append(
            AdsSensor(
                ads_hub,
                ads_var,
                ads_type,
                name,
                factor,
                device_class,
                state_class,
                unit_of_measurement,
                unique_id,
            )
        )

    async_add_entities(entities)


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
        unique_id: str,
    ) -> None:
        """Initialize AdsSensor entity."""
        super().__init__(ads_hub, name, ads_var)
        self._ads_type = ads_type
        self._factor = factor
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._unique_id = unique_id

    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await self.async_initialize_device(
            self._ads_var,
            ADS_TYPEMAP[self._ads_type],
            STATE_KEY_STATE,
            self._factor,
        )

    @property
    def native_value(self) -> StateType:
        """Return the state of the device."""
        return self._state_dict[STATE_KEY_STATE]

    @property
    def unique_id(self):
        """Return the unique id of the device."""
        return self._unique_id
