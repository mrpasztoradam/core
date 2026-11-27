"""Entities of the devices added in the UI.

A device subentry holds its entities as components, each for one platform, as
MQTT models them. Every platform adds the components that are its own.
"""

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import CONF_NAME, CONF_PLATFORM, EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_COMPONENTS,
    CONF_ENTITY_CATEGORY,
    CONF_MODEL,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
)
from .entity import AdsEntity
from .hub import AdsConfigEntry, AdsHub

# Builds the entity of a component from the hub, the component's data and the
# keywords every entity of a device subentry takes.
type EntityFactory = Callable[[AdsHub, Mapping[str, Any], dict[str, Any]], AdsEntity]


@callback
def async_setup_subentry_entities(
    hass: HomeAssistant,
    entry: AdsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: Platform,
    factory: EntityFactory,
) -> None:
    """Add the platform's entities of the entry's devices, now and when added."""
    hub = entry.runtime_data
    hub_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert hub_device is not None
    known: set[str] = set()

    @callback
    def _async_add_new_entities() -> None:
        # Forget deleted subentries, so their IDs are not kept until unload.
        known.intersection_update(entry.subentries)
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_DEVICE or subentry_id in known:
                continue
            known.add(subentry_id)
            device_info = DeviceInfo(
                identifiers={(DOMAIN, subentry_id)},
                name=subentry.title,
                model=subentry.data.get(CONF_MODEL),
                via_device_id=hub_device.id,
            )
            entities = [
                factory(
                    hub,
                    component,
                    {
                        "unique_id": f"{subentry_id}_{component_id}",
                        "device_info": device_info,
                        "entity_name": component.get(CONF_NAME),
                        "entity_category": (
                            EntityCategory(category)
                            if (category := component.get(CONF_ENTITY_CATEGORY))
                            else None
                        ),
                    },
                )
                for component_id, component in subentry.data[CONF_COMPONENTS].items()
                if component[CONF_PLATFORM] == platform
            ]
            if entities:
                async_add_entities(entities, config_subentry_id=subentry_id)

    @callback
    def _async_entry_changed(
        change: ConfigEntryChange, changed_entry: ConfigEntry
    ) -> None:
        # Adding a subentry does not reload the entry.
        if change is ConfigEntryChange.UPDATED and changed_entry is entry:
            _async_add_new_entities()

    _async_add_new_entities()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_CONFIG_ENTRY_CHANGED, _async_entry_changed
        )
    )
