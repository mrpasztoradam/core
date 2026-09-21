"""Support for ADS binary sensors."""

from typing import override

import probatio
import pyads

from homeassistant.components.binary_sensor import (
    DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as BINARY_SENSOR_PLATFORM_SCHEMA,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, DATA_ADS, STATE_KEY_STATE
from .entity import AdsEntity, AdsHubEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS binary sensor"
PLATFORM_SCHEMA = BINARY_SENSOR_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_ADS_VAR): cv.string,
        probatio.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        probatio.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Binary Sensor platform for ADS."""
    ads_hub = hass.data[DATA_ADS]

    if discovery_info is not None:
        add_entities([AdsConnectionBinarySensor(ads_hub)])
        return

    ads_var: str = config[CONF_ADS_VAR]
    name: str = config[CONF_NAME]
    device_class: BinarySensorDeviceClass | None = config.get(CONF_DEVICE_CLASS)

    ads_sensor = AdsBinarySensor(ads_hub, name, ads_var, device_class)
    add_entities([ads_sensor])


class AdsBinarySensor(AdsEntity, BinarySensorEntity):
    """Representation of ADS binary sensors."""

    def __init__(
        self,
        ads_hub: AdsHub,
        name: str,
        ads_var: str,
        device_class: BinarySensorDeviceClass | None,
    ) -> None:
        """Initialize ADS binary sensor."""
        super().__init__(ads_hub, name, ads_var)
        self._attr_device_class = device_class

    @override
    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await super().async_added_to_hass()
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True if the entity is on."""
        return self._state_dict[STATE_KEY_STATE]


class AdsConnectionBinarySensor(AdsHubEntity, BinarySensorEntity):
    """Representation of whether the ADS device is answering.

    Deliberately not whether the integration can use it: a device whose PLC
    program has stopped is still perfectly reachable, and reporting that as a
    lost connection sends whoever is looking after the wrong problem. What the
    device is doing belongs to the state sensor.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, ads_hub: AdsHub) -> None:
        """Initialize the ADS connection binary sensor."""
        super().__init__(ads_hub, "connection")

    @property
    @override
    def is_on(self) -> bool:
        """Return True while the device is answering."""
        return self._ads_hub.reachable
