"""Proof of concept: ADS numbers for numeric commands, such as setpoints."""

from collections.abc import Mapping
from typing import Any, override

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import CONF_UNIT_OF_MEASUREMENT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ADS_TYPEMAP, CONF_ADS_TYPE, CONF_ADS_VAR, STATE_KEY_STATE, AdsType
from .entity import AdsEntity
from .hub import AdsConfigEntry, AdsHub
from .subentry import async_setup_subentry_entities

PARALLEL_UPDATES = 0

# The range a variable of each type can hold, as far as a number entity
# needs one.
TYPE_RANGES = {
    AdsType.BYTE: (0, 255, 1),
    AdsType.USINT: (0, 255, 1),
    AdsType.SINT: (-128, 127, 1),
    AdsType.INT: (-32768, 32767, 1),
    AdsType.UINT: (0, 65535, 1),
    AdsType.DINT: (-2147483648, 2147483647, 1),
    AdsType.UDINT: (0, 4294967295, 1),
    AdsType.REAL: (-1e6, 1e6, 0.1),
    AdsType.LREAL: (-1e6, 1e6, 0.1),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the numbers of the devices added in the UI."""

    def _create(
        hub: AdsHub, component: Mapping[str, Any], kwargs: dict[str, Any]
    ) -> AdsNumber:
        return AdsNumber(hub, component, **kwargs)

    async_setup_subentry_entities(
        hass, entry, async_add_entities, Platform.NUMBER, _create
    )


class AdsNumber(AdsEntity, NumberEntity):
    """A numeric variable Home Assistant writes, showing what the PLC holds."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self, ads_hub: AdsHub, component: Mapping[str, Any], **entity_kwargs: Any
    ) -> None:
        """Initialize the number."""
        super().__init__(ads_hub, "", component[CONF_ADS_VAR], **entity_kwargs)
        self._ads_type = AdsType(component[CONF_ADS_TYPE])
        low, high, step = TYPE_RANGES.get(self._ads_type, (-1e6, 1e6, 1))
        self._attr_native_min_value = component.get("min", low)
        self._attr_native_max_value = component.get("max", high)
        self._attr_native_step = component.get("step", step)
        self._attr_native_unit_of_measurement = component.get(CONF_UNIT_OF_MEASUREMENT)

    @override
    async def async_subscribe(self) -> None:
        """Subscribe to the PLC variable."""
        await self.async_initialize_device(self._ads_var, ADS_TYPEMAP[self._ads_type])

    @property
    @override
    def native_value(self) -> float | None:
        """Return the value the PLC holds."""
        return self._state_dict[STATE_KEY_STATE]

    @override
    def set_native_value(self, value: float) -> None:
        """Write the value to the PLC."""
        if self._ads_type not in (AdsType.REAL, AdsType.LREAL):
            value = int(value)
        self._ads_hub.write_by_name(self._ads_var, value, ADS_TYPEMAP[self._ads_type])
