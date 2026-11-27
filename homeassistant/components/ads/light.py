"""Support for ADS light sources."""

from collections.abc import Mapping
import struct
from typing import Any, override

import probatio
import pyads

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGBW_COLOR,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    PLATFORM_SCHEMA as LIGHT_PLATFORM_SCHEMA,
    ColorMode,
    LightEntity,
    filter_supported_color_modes,
)
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, CONF_INTERFACE, CONF_OBJECT, STATE_KEY_STATE
from .entity import AdsEntity
from .hub import AdsConfigEntry, AdsHub, async_wait_for_hub
from .subentry import async_setup_subentry_entities

CONF_ADS_VAR_BRIGHTNESS = "adsvar_brightness"
CONF_ADS_VAR_COLOR_TEMP_KELVIN = "adsvar_color_temp_kelvin"
CONF_MIN_COLOR_TEMP_KELVIN = "min_color_temp_kelvin"
CONF_MAX_COLOR_TEMP_KELVIN = "max_color_temp_kelvin"
STATE_KEY_BRIGHTNESS = "brightness"
STATE_KEY_COLOR_TEMP_KELVIN = "color_temp_kelvin"

DEFAULT_NAME = "ADS Light"
PLATFORM_SCHEMA = LIGHT_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_ADS_VAR): cv.string,
        probatio.Optional(CONF_ADS_VAR_BRIGHTNESS): cv.string,
        probatio.Optional(CONF_ADS_VAR_COLOR_TEMP_KELVIN): cv.string,
        probatio.Optional(CONF_MIN_COLOR_TEMP_KELVIN): cv.positive_int,
        probatio.Optional(CONF_MAX_COLOR_TEMP_KELVIN): cv.positive_int,
        probatio.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the light platform for ADS."""
    ads_hub = await async_wait_for_hub(hass)

    ads_var_enable: str = config[CONF_ADS_VAR]
    ads_var_brightness: str | None = config.get(CONF_ADS_VAR_BRIGHTNESS)
    ads_var_color_temp_kelvin: str | None = config.get(CONF_ADS_VAR_COLOR_TEMP_KELVIN)
    min_color_temp_kelvin: int | None = config.get(CONF_MIN_COLOR_TEMP_KELVIN)
    max_color_temp_kelvin: int | None = config.get(CONF_MAX_COLOR_TEMP_KELVIN)
    name: str = config[CONF_NAME]

    async_add_entities(
        [
            AdsLight(
                ads_hub,
                ads_var_enable,
                ads_var_brightness,
                ads_var_color_temp_kelvin,
                min_color_temp_kelvin,
                max_color_temp_kelvin,
                name,
            )
        ]
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the lights of the devices added in the UI."""

    def _create(
        hub: AdsHub, component: Mapping[str, Any], kwargs: dict[str, Any]
    ) -> AdsEntity:
        if CONF_OBJECT in component:
            if component.get(CONF_INTERFACE) == "I_HA_RgbwLight":
                return AdsObjectRgbwLight(hub, component[CONF_OBJECT], **kwargs)
            return AdsObjectLight(hub, component[CONF_OBJECT], **kwargs)
        return AdsLight(
            hub,
            component[CONF_ADS_VAR],
            component.get(CONF_ADS_VAR_BRIGHTNESS),
            None,
            None,
            None,
            "",
            **kwargs,
        )

    async_setup_subentry_entities(
        hass, entry, async_add_entities, Platform.LIGHT, _create
    )


class AdsLight(AdsEntity, LightEntity):
    """Representation of ADS light."""

    def __init__(
        self,
        ads_hub: AdsHub,
        ads_var_enable: str,
        ads_var_brightness: str | None,
        ads_var_color_temp_kelvin: str | None,
        min_color_temp_kelvin: int | None,
        max_color_temp_kelvin: int | None,
        name: str,
        **entity_kwargs: Any,
    ) -> None:
        """Initialize AdsLight entity."""
        super().__init__(ads_hub, name, ads_var_enable, **entity_kwargs)
        self._state_dict[STATE_KEY_BRIGHTNESS] = None
        self._state_dict[STATE_KEY_COLOR_TEMP_KELVIN] = None
        self._ads_var_brightness = ads_var_brightness
        self._ads_var_color_temp_kelvin = ads_var_color_temp_kelvin

        # Determine supported color modes
        color_modes = {ColorMode.ONOFF}
        if ads_var_brightness is not None:
            color_modes.add(ColorMode.BRIGHTNESS)
        if ads_var_color_temp_kelvin is not None:
            color_modes.add(ColorMode.COLOR_TEMP)

        self._attr_supported_color_modes = filter_supported_color_modes(color_modes)
        self._attr_color_mode = next(iter(self._attr_supported_color_modes))

        # Set color temperature range
        # (static config values take precedence over defaults)
        if ads_var_color_temp_kelvin is not None:
            self._attr_min_color_temp_kelvin = (
                min_color_temp_kelvin
                if min_color_temp_kelvin is not None
                else DEFAULT_MIN_KELVIN
            )
            self._attr_max_color_temp_kelvin = (
                max_color_temp_kelvin
                if max_color_temp_kelvin is not None
                else DEFAULT_MAX_KELVIN
            )

    @override
    async def async_subscribe(self) -> None:
        """Subscribe to the PLC variables of this entity."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

        if self._ads_var_brightness is not None:
            await self.async_initialize_device(
                self._ads_var_brightness,
                pyads.PLCTYPE_UINT,
                STATE_KEY_BRIGHTNESS,
            )

        if self._ads_var_color_temp_kelvin is not None:
            await self.async_initialize_device(
                self._ads_var_color_temp_kelvin,
                pyads.PLCTYPE_UINT,
                STATE_KEY_COLOR_TEMP_KELVIN,
            )

    @property
    @override
    def brightness(self) -> int | None:
        """Return the brightness of the light (0..255)."""
        return self._state_dict[STATE_KEY_BRIGHTNESS]

    @property
    @override
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        return self._state_dict[STATE_KEY_COLOR_TEMP_KELVIN]

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the entity is on."""
        return self._state_dict[STATE_KEY_STATE]

    @override
    def turn_on(self, **kwargs: Any) -> None:
        """Turn the light on or set a specific dimmer value."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        self._ads_hub.write_by_name(self._ads_var, True, pyads.PLCTYPE_BOOL)

        if self._ads_var_brightness is not None and brightness is not None:
            self._ads_hub.write_by_name(
                self._ads_var_brightness, brightness, pyads.PLCTYPE_UINT
            )

        if self._ads_var_color_temp_kelvin is not None and color_temp is not None:
            self._ads_hub.write_by_name(
                self._ads_var_color_temp_kelvin, color_temp, pyads.PLCTYPE_UINT
            )

    @override
    def turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        self._ads_hub.write_by_name(self._ads_var, False, pyads.PLCTYPE_BOOL)


class AdsObjectLight(AdsEntity, LightEntity):
    """A light object of the PLC program (I_HA_Light).

    The state is the object's IsOn and Brightness properties, which follow the
    real light, and the commands call its TurnOn and TurnOff methods.
    """

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, ads_hub: AdsHub, path: str, **entity_kwargs: Any) -> None:
        """Initialize the light object."""
        super().__init__(ads_hub, "", path, **entity_kwargs)
        self._state_dict[STATE_KEY_BRIGHTNESS] = None

    @override
    async def async_subscribe(self) -> None:
        """Follow the object's properties."""
        await self.async_initialize_device(f"{self._ads_var}.IsOn", pyads.PLCTYPE_BOOL)
        await self.async_initialize_device(
            f"{self._ads_var}.Brightness", pyads.PLCTYPE_USINT, STATE_KEY_BRIGHTNESS
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Return whether the light is on."""
        return self._state_dict[STATE_KEY_STATE]

    @property
    @override
    def brightness(self) -> int | None:
        """Return the light's actual brightness."""
        return self._state_dict[STATE_KEY_BRIGHTNESS]

    @override
    def turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, at a brightness if given (0 = the PLC's own)."""
        brightness = kwargs.get(ATTR_BRIGHTNESS, 0)
        self._ads_hub.call_method(
            self._ads_var, "TurnOn", struct.pack("<B", brightness), return_size=1
        )

    @override
    def turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        self._ads_hub.call_method(self._ads_var, "TurnOff", b"", return_size=1)


STATE_KEY_COLOR = "color"
STATE_KEY_COLOR_TEMP = "color_temp"
STATE_KEY_MODE = "mode"
# I_HA_RgbwLight.Mode
MODE_COLOR_TEMP = 2


class AdsObjectRgbwLight(AdsObjectLight):
    """An RGBW light object of the PLC program (I_HA_RgbwLight).

    The colour is one packed property (16#WWBBGGRR), so a colour change
    arrives in one notification; colour and level are set in one call.
    """

    _attr_supported_color_modes = {ColorMode.RGBW, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 6500

    def __init__(self, ads_hub: AdsHub, path: str, **entity_kwargs: Any) -> None:
        """Initialize the RGBW light object."""
        super().__init__(ads_hub, path, **entity_kwargs)
        self._state_dict[STATE_KEY_COLOR] = None
        self._state_dict[STATE_KEY_COLOR_TEMP] = None
        self._state_dict[STATE_KEY_MODE] = None

    @override
    async def async_subscribe(self) -> None:
        """Follow the object's properties."""
        await super().async_subscribe()
        await self.async_initialize_device(
            f"{self._ads_var}.Color", pyads.PLCTYPE_UDINT, STATE_KEY_COLOR
        )
        await self.async_initialize_device(
            f"{self._ads_var}.ColorTemp", pyads.PLCTYPE_UINT, STATE_KEY_COLOR_TEMP
        )
        await self.async_initialize_device(
            f"{self._ads_var}.Mode", pyads.PLCTYPE_USINT, STATE_KEY_MODE
        )

    @property
    @override
    def color_mode(self) -> ColorMode:
        """Return whether the light shows a colour or a white temperature."""
        if self._state_dict[STATE_KEY_MODE] == MODE_COLOR_TEMP:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGBW

    @property
    @override
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Return the actual colour."""
        if (packed := self._state_dict[STATE_KEY_COLOR]) is None:
            return None
        return (
            packed & 0xFF,
            packed >> 8 & 0xFF,
            packed >> 16 & 0xFF,
            packed >> 24 & 0xFF,
        )

    @property
    @override
    def color_temp_kelvin(self) -> int | None:
        """Return the actual white temperature, if in that mode."""
        return self._state_dict[STATE_KEY_COLOR_TEMP] or None

    @override
    def turn_on(self, **kwargs: Any) -> None:
        """Turn on, setting the colour or white temperature with the level."""
        brightness = kwargs.get(ATTR_BRIGHTNESS, 0)
        if (rgbw := kwargs.get(ATTR_RGBW_COLOR)) is not None:
            self._ads_hub.call_method(
                self._ads_var,
                "SetColor",
                struct.pack("<5B", *rgbw, brightness),
                return_size=1,
            )
        elif (kelvin := kwargs.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
            self._ads_hub.call_method(
                self._ads_var,
                "SetColorTemp",
                struct.pack("<HB", kelvin, brightness),
                return_size=1,
            )
        else:
            super().turn_on(**kwargs)
