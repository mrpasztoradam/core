"""Support for ADS switch platform."""

from collections.abc import Mapping
from typing import Any, override

import probatio
import pyads

from homeassistant.components.switch import (
    PLATFORM_SCHEMA as SWITCH_PLATFORM_SCHEMA,
    SwitchEntity,
)
from homeassistant.const import CONF_NAME, Platform
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

DEFAULT_NAME = "ADS Switch"

# Push based, and the hub serialises the writes.
PARALLEL_UPDATES = 0

PLATFORM_SCHEMA = SWITCH_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_ADS_VAR): cv.string,
        probatio.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up switch platform for ADS."""
    ads_hub = await async_wait_for_hub(hass)

    name: str = config[CONF_NAME]
    ads_var: str = config[CONF_ADS_VAR]

    async_add_entities([AdsSwitch(ads_hub, name, ads_var)])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches of the devices added in the UI."""

    def _create(
        hub: AdsHub, component: Mapping[str, Any], kwargs: dict[str, Any]
    ) -> AdsSwitch:
        return AdsSwitch(hub, "", component[CONF_ADS_VAR], **kwargs)

    async_setup_subentry_entities(
        hass, entry, async_add_entities, Platform.SWITCH, _create
    )


class AdsSwitch(AdsEntity, SwitchEntity):
    """Representation of an ADS switch device."""

    @override
    async def async_subscribe(self) -> None:
        """Subscribe to the PLC variables of this entity."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the entity is on."""
        return self._state_dict[STATE_KEY_STATE]

    @override
    def turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._ads_hub.write_by_name(self._ads_var, True, pyads.PLCTYPE_BOOL)

    @override
    def turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._ads_hub.write_by_name(self._ads_var, False, pyads.PLCTYPE_BOOL)
