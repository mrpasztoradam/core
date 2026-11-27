"""Config flow for the ADS integration."""

from collections.abc import Mapping
from functools import partial
import logging
from types import MappingProxyType
from typing import Any, override
from uuid import uuid4

import probatio
import pyads

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_DEVICE,
    CONF_IP_ADDRESS,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_PORT,
    Platform,
)
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, floor_registry as fr
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    BOOL_SYMBOL_TYPES,
    CONF_ADS_VAR,
    CONF_AREA,
    CONF_COMPONENTS,
    CONF_INTERFACE,
    CONF_MODEL,
    CONF_OBJECT,
    DEFAULT_PORT,
    DOMAIN,
    ELEMENTARY_SYMBOL_TYPES,
    SUBENTRY_PLATFORMS,
    SUBENTRY_TYPE_DEVICE,
)
from .hub import AdsConfigEntry, check_reachable
from .plc_objects import INTERFACES, PlcObject

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_DEVICE): str,
        probatio.Optional(CONF_IP_ADDRESS): str,
        probatio.Required(CONF_PORT, default=DEFAULT_PORT): probatio.Port(),
    }
)


STEP_ENTITY_DATA_SCHEMA = probatio.Schema(
    {
        # The device is the user's own; nothing on the PLC names it.
        # pylint: disable-next=home-assistant-config-flow-name-field
        probatio.Required(CONF_NAME): TextSelector(),
        probatio.Required(CONF_ADS_VAR): TextSelector(),
    }
)

# ADS error code for a variable the device does not know.
ADS_ERROR_SYMBOL_NOT_FOUND = 1808


def _drop_blank_optionals(data: dict[str, Any]) -> dict[str, Any]:
    """Drop the IP address if it was left blank.

    A cleared field is submitted as an empty string, which pyads would take
    as an explicit value instead of deriving it from the AMS NetID.
    """
    return {
        key: value for key, value in data.items() if key != CONF_IP_ADDRESS or value
    }


def _is_valid_net_id(net_id: str) -> bool:
    """Return whether the AMS NetID is six numbers from 0 to 255, separated by dots."""
    parts = net_id.split(".")
    return len(parts) == 6 and all(
        # isdigit() alone accepts digits like "²" that int() rejects.
        part.isascii() and part.isdigit() and len(part) <= 3 and int(part) <= 255
        for part in parts
    )


def _host(data: Mapping[str, Any]) -> str:
    """Return the address pyads connects to, derived the same way pyads does."""
    return data.get(CONF_IP_ADDRESS) or ".".join(data[CONF_DEVICE].split(".")[:4])


def _validate_connection(data: dict[str, Any], *, reachable_only: bool) -> None:
    """Open a connection and read the device state.

    With reachable_only, only check the device accepts a TCP connection.
    """
    check_reachable(data[CONF_DEVICE], data.get(CONF_IP_ADDRESS))
    if reachable_only:
        return
    client = pyads.Connection(
        data[CONF_DEVICE], data[CONF_PORT], data.get(CONF_IP_ADDRESS)
    )
    client.open()
    try:
        # pyads returns None instead of raising when the port is closed.
        if client.read_state() is None:
            raise pyads.ADSError(err_code=1864)
    finally:
        client.close()


class AdsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ADS."""

    VERSION = 1

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries the entry supports."""
        return {SUBENTRY_TYPE_DEVICE: AdsDeviceSubentryFlow}

    def _route_in_use(self, data: dict[str, Any]) -> bool:
        """Return whether the loaded hub holds a route to the NetID elsewhere.

        The AMS router keeps one route per NetID, so on Linux a second
        connection to it at another address is refused while the hub runs.
        The reload connects to the new address anyway.
        """
        if self.source != SOURCE_RECONFIGURE:
            return False
        entry = self._get_reconfigure_entry()
        return (
            entry.state is ConfigEntryState.LOADED
            and entry.data[CONF_DEVICE] == data[CONF_DEVICE]
            and _host(entry.data) != _host(data)
        )

    async def _async_validate(self, data: dict[str, Any]) -> str | None:
        """Validate the connection and return an error key on failure."""
        try:
            await self.hass.async_add_executor_job(
                partial(
                    _validate_connection,
                    data,
                    reachable_only=self._route_in_use(data),
                )
            )
        except pyads.ADSError, OSError, RuntimeError:
            # A device that does not answer raises OSError and the AMS router
            # refusing to open a port raises RuntimeError; setup treats both as
            # connectivity failures too.
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            return "unknown"
        return None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _drop_blank_optionals(user_input)
            if not _is_valid_net_id(user_input[CONF_DEVICE]):
                # Checked first: without an IP address the NetID also names
                # the host, and a malformed one would only fail to connect.
                errors[CONF_DEVICE] = "invalid_net_id"
            elif (error := await self._async_validate(user_input)) is None:
                if self.source == SOURCE_RECONFIGURE:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(),
                        title=user_input[CONF_DEVICE],
                        data=user_input,
                    )
                return self.async_create_entry(
                    title=user_input[CONF_DEVICE], data=user_input
                )
            else:
                errors["base"] = error

        suggested_values = user_input
        if suggested_values is None and self.source == SOURCE_RECONFIGURE:
            suggested_values = dict(self._get_reconfigure_entry().data)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, suggested_values
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of the ADS connection."""
        return await self.async_step_user(user_input)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import the ADS connection from configuration.yaml.

        The connection is not tested here: a device that is offline when Home
        Assistant upgrades would otherwise leave the YAML entities without a
        connection. Setting up the entry retries until the device answers.
        """
        import_data = _drop_blank_optionals(import_data)
        if not _is_valid_net_id(import_data[CONF_DEVICE]):
            return self.async_abort(reason="invalid_net_id")
        return self.async_create_entry(title=import_data[CONF_DEVICE], data=import_data)


def _base_symbol_type(symbol_type: str) -> str:
    """Return the type without its length, such as STRING for STRING(80)."""
    return symbol_type.partition("(")[0].strip().upper()


class AdsDeviceSubentryFlow(ConfigSubentryFlow):
    """Add or change a device made of entities for PLC variables.

    The subentry holds the entities as components, keyed by a component ID,
    so a device can later carry several entities, such as those of a building
    automation object.
    """

    _name: str
    _component_id: str
    _component: dict[str, Any]
    _objects: list[PlcObject]
    # Shown by the step that asks to save the variable unchecked.
    _confirm_placeholders: dict[str, str]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask what kind of entity the device has."""
        self._component_id = uuid4().hex
        return self.async_show_menu(
            step_id="user", menu_options=["objects", *SUBENTRY_PLATFORMS]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the device's entity."""
        subentry = self._get_reconfigure_subentry()
        # Every device has a single entity so far.
        self._component_id, component = next(
            iter(subentry.data[CONF_COMPONENTS].items())
        )
        return await self._async_step_platform(component[CONF_PLATFORM], None)

    async def async_step_objects(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Find the objects the PLC program defines for Home Assistant."""
        entry: AdsConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="objects_not_loaded")
        try:
            self._objects = await self.hass.async_add_executor_job(
                entry.runtime_data.discover_objects
            )
        except pyads.ADSError, OSError, RuntimeError:
            _LOGGER.exception("Discovering the objects failed")
            return self.async_abort(reason="objects_failed")
        return await self.async_step_objects_select()

    async def async_step_objects_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose which objects to add, then add them in their rooms."""
        entry = self._get_entry()
        known = {subentry.unique_id for subentry in entry.subentries.values()}
        new = [obj for obj in self._objects if f"obj:{obj.path}" not in known]
        if not new:
            return self.async_abort(
                reason="objects_nothing_new",
                description_placeholders={"found": str(len(self._objects))},
            )
        if user_input is not None:
            chosen = [obj for obj in new if obj.path in user_input["objects"]]
            if user_input["areas"]:
                self._async_create_areas(chosen)
            for obj in chosen:
                data: dict[str, Any] = {
                    CONF_OBJECT: obj.path,
                    CONF_MODEL: obj.fb_type,
                    CONF_COMPONENTS: {
                        uuid4().hex: {
                            CONF_PLATFORM: INTERFACES[obj.interface],
                            CONF_OBJECT: obj.path,
                            CONF_INTERFACE: obj.interface,
                        }
                    },
                }
                if user_input["areas"] and obj.room:
                    data[CONF_AREA] = obj.room
                self.hass.config_entries.async_add_subentry(
                    entry,
                    ConfigSubentry(
                        subentry_type=SUBENTRY_TYPE_DEVICE,
                        title=obj.name,
                        data=MappingProxyType(data),
                        unique_id=f"obj:{obj.path}",
                    ),
                )
            return self.async_abort(
                reason="objects_successful",
                description_placeholders={"added": str(len(chosen))},
            )
        return self.async_show_form(
            step_id="objects_select",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        "objects", default=[obj.path for obj in new]
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=obj.path,
                                    label=" · ".join(
                                        part
                                        for part in (obj.floor, obj.room, obj.name)
                                        if part
                                    ),
                                )
                                for obj in new
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    probatio.Required("areas", default=True): BooleanSelector(),
                }
            ),
            description_placeholders={
                "found": str(len(self._objects)),
                "new": str(len(new)),
                "interfaces": ", ".join(sorted({obj.interface for obj in new})),
            },
        )

    @callback
    def _async_create_areas(self, objects: list[PlcObject]) -> None:
        """Create the objects' floors, and their rooms as areas on them."""
        floor_reg = fr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        for obj in objects:
            if not obj.room:
                continue
            floor_id = None
            if obj.floor:
                floor = floor_reg.async_get_floor_by_name(
                    obj.floor
                ) or floor_reg.async_create(obj.floor)
                floor_id = floor.floor_id
            area = area_reg.async_get_area_by_name(obj.room)
            if area is None:
                area_reg.async_create(obj.room, floor_id=floor_id)
            elif area.floor_id is None and floor_id is not None:
                area_reg.async_update(area.id, floor_id=floor_id)

    async def async_step_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the switch's name and variable."""
        return await self._async_step_platform(Platform.SWITCH, user_input)

    async def _async_step_platform(
        self, platform: Platform, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Ask for the name and variable of an entity of the platform."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            self._name = user_input[CONF_NAME]
            self._component = {
                CONF_PLATFORM: platform,
                CONF_ADS_VAR: user_input[CONF_ADS_VAR],
            }
            self._confirm_placeholders = {"adsvar": user_input[CONF_ADS_VAR]}
            try:
                unverified_step = await self._async_check_variable(
                    user_input[CONF_ADS_VAR]
                )
            except _WrongTypeError as err:
                errors[CONF_ADS_VAR] = "wrong_type"
                placeholders["type"] = err.symbol_type
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if unverified_step is None:
                    return self._async_finish()
                return await self._async_step_confirm(unverified_step, None)

        suggested_values = user_input
        if suggested_values is None and self.source == SOURCE_RECONFIGURE:
            subentry = self._get_reconfigure_subentry()
            component = subentry.data[CONF_COMPONENTS][self._component_id]
            suggested_values = {
                CONF_NAME: subentry.title,
                CONF_ADS_VAR: component[CONF_ADS_VAR],
            }

        return self.async_show_form(
            step_id=platform,
            data_schema=self.add_suggested_values_to_schema(
                STEP_ENTITY_DATA_SCHEMA, suggested_values
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_check_variable(self, ads_var: str) -> str | None:
        """Check the variable is a BOOL on the PLC.

        Returns None when it is, or the step that asks to save it unchecked.
        Raises _WrongTypeError when it is known to be something else.
        """
        entry: AdsConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return "confirm_not_loaded"
        try:
            symbol_type = await self.hass.async_add_executor_job(
                entry.runtime_data.get_symbol_type, ads_var
            )
        except pyads.ADSError as err:
            if err.err_code == ADS_ERROR_SYMBOL_NOT_FOUND:
                return "confirm_not_found"
            self._confirm_placeholders["error"] = str(err)
            return "confirm_cannot_connect"
        except RuntimeError as err:
            # The AMS router refusing the request.
            self._confirm_placeholders["error"] = str(err)
            return "confirm_cannot_connect"
        base_type = _base_symbol_type(symbol_type)
        if base_type in BOOL_SYMBOL_TYPES:
            return None
        if base_type in ELEMENTARY_SYMBOL_TYPES:
            raise _WrongTypeError(symbol_type)
        self._confirm_placeholders["type"] = symbol_type
        return "confirm_unknown_type"

    async def _async_step_confirm(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Ask whether to save a variable that could not be checked."""
        if user_input is not None:
            return self._async_finish()
        return self.async_show_form(
            step_id=step_id, description_placeholders=self._confirm_placeholders
        )

    async def async_step_confirm_not_loaded(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask to save the entity while the connection is not set up."""
        return await self._async_step_confirm("confirm_not_loaded", user_input)

    async def async_step_confirm_not_found(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask to save an entity for a variable the PLC does not know."""
        return await self._async_step_confirm("confirm_not_found", user_input)

    async def async_step_confirm_cannot_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask to save the entity while the PLC does not answer."""
        return await self._async_step_confirm("confirm_cannot_connect", user_input)

    async def async_step_confirm_unknown_type(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask to save an entity for a variable whose type is not a known BOOL."""
        return await self._async_step_confirm("confirm_unknown_type", user_input)

    @callback
    def _async_finish(self) -> SubentryFlowResult:
        """Create or update the subentry."""
        data = {CONF_COMPONENTS: {self._component_id: self._component}}
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=self._name,
                data=data,
            )
        return self.async_create_entry(title=self._name, data=data)


class _WrongTypeError(Exception):
    """The variable is known to be a type a switch cannot write."""

    def __init__(self, symbol_type: str) -> None:
        super().__init__(symbol_type)
        self.symbol_type = symbol_type
