"""Support for ADS switch platform."""

from typing import Any, override

import probatio
import pyads

from homeassistant.components.switch import (
    PLATFORM_SCHEMA as SWITCH_PLATFORM_SCHEMA,
    SwitchEntity,
)
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, DOMAIN, STATE_KEY_STATE, SUBENTRY_TYPE_SWITCH
from .entity import AdsEntity
from .hub import AdsConfigEntry, async_wait_for_hub

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
    """Set up the switches of the entry's subentries."""
    hub = entry.runtime_data
    hub_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert hub_device is not None
    known: set[str] = set()

    @callback
    def _async_add_new_switches() -> None:
        # Forget deleted subentries, so their IDs are not kept until unload.
        known.intersection_update(entry.subentries)
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_SWITCH or subentry_id in known:
                continue
            known.add(subentry_id)
            switch = AdsSwitch(
                hub,
                subentry.title,
                subentry.data[CONF_ADS_VAR],
                unique_id=subentry_id,
                device_info=DeviceInfo(
                    identifiers={(DOMAIN, subentry_id)},
                    name=subentry.title,
                    via_device_id=hub_device.id,
                ),
            )
            async_add_entities([switch], config_subentry_id=subentry_id)

    @callback
    def _async_entry_changed(
        change: ConfigEntryChange, changed_entry: ConfigEntry
    ) -> None:
        # Adding a subentry does not reload the entry.
        if change is ConfigEntryChange.UPDATED and changed_entry is entry:
            _async_add_new_switches()

    _async_add_new_switches()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_CONFIG_ENTRY_CHANGED, _async_entry_changed
        )
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
