"""Proof of concept: ADS buttons for commands the PLC resets itself."""

from collections.abc import Mapping
from typing import Any, override

import pyads

from homeassistant.components.button import ButtonEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ADS_VAR
from .entity import AdsEntity
from .hub import AdsConfigEntry, AdsHub
from .subentry import async_setup_subentry_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons of the devices added in the UI."""

    def _create(
        hub: AdsHub, component: Mapping[str, Any], kwargs: dict[str, Any]
    ) -> AdsButton:
        return AdsButton(hub, "", component[CONF_ADS_VAR], **kwargs)

    async_setup_subentry_entities(
        hass, entry, async_add_entities, Platform.BUTTON, _create
    )


class AdsButton(AdsEntity, ButtonEntity):
    """A BOOL command that the PLC resets once it has acted on it."""

    @override
    async def async_subscribe(self) -> None:
        """Do not subscribe: the variable is only ever set by pressing."""

    @property
    @override
    def available(self) -> bool:
        """Return True; a button has no value to wait for."""
        return True

    @override
    def press(self) -> None:
        """Set the command; the PLC resets it."""
        self._ads_hub.write_by_name(self._ads_var, True, pyads.PLCTYPE_BOOL)
