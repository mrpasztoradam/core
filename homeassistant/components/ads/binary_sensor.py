"""Support for ADS binary sensors."""

from collections.abc import Mapping
from typing import Any, override

import probatio
import pyads

from homeassistant.components.binary_sensor import (
    DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as BINARY_SENSOR_PLATFORM_SCHEMA,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, STATE_KEY_STATE
from .entity import AdsEntity
from .hub import AdsConfigEntry, AdsHub, async_wait_for_hub
from .subentry import async_setup_subentry_entities

DEFAULT_NAME = "ADS binary sensor"
PLATFORM_SCHEMA = BINARY_SENSOR_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_ADS_VAR): cv.string,
        probatio.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        probatio.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Binary Sensor platform for ADS."""
    ads_hub = await async_wait_for_hub(hass)

    ads_var: str = config[CONF_ADS_VAR]
    name: str = config[CONF_NAME]
    device_class: BinarySensorDeviceClass | None = config.get(CONF_DEVICE_CLASS)

    ads_sensor = AdsBinarySensor(ads_hub, name, ads_var, device_class)
    async_add_entities([ads_sensor])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of the devices added in the UI."""

    def _create(
        hub: AdsHub, component: Mapping[str, Any], kwargs: dict[str, Any]
    ) -> AdsBinarySensor:
        sensor = AdsBinarySensor(hub, "", component[CONF_ADS_VAR], None, **kwargs)
        # Unlike in YAML, no device class means none, not moving.
        device_class = component.get(CONF_DEVICE_CLASS)
        sensor._attr_device_class = (  # noqa: SLF001
            BinarySensorDeviceClass(device_class) if device_class else None
        )
        return sensor

    async_setup_subentry_entities(
        hass, entry, async_add_entities, Platform.BINARY_SENSOR, _create
    )


class AdsBinarySensor(AdsEntity, BinarySensorEntity):
    """Representation of ADS binary sensors."""

    def __init__(
        self,
        ads_hub: AdsHub,
        name: str,
        ads_var: str,
        device_class: BinarySensorDeviceClass | None,
        **entity_kwargs: Any,
    ) -> None:
        """Initialize ADS binary sensor."""
        super().__init__(ads_hub, name, ads_var, **entity_kwargs)
        self._attr_device_class = device_class or BinarySensorDeviceClass.MOVING

    @override
    async def async_subscribe(self) -> None:
        """Subscribe to the PLC variables of this entity."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the entity is on."""
        return self._state_dict[STATE_KEY_STATE]
