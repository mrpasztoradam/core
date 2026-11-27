"""Test the ADS config flow."""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pyads
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.ads.const import DOMAIN
from homeassistant.config_entries import (
    SOURCE_IMPORT,
    SOURCE_USER,
    ConfigEntryState,
    ConfigSubentryDataWithId,
)
from homeassistant.const import CONF_DEVICE, CONF_IP_ADDRESS, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from . import ADS_CONFIG
from .const import (
    AMS_NET_ID,
    IP_ADDRESS,
    PORT,
    SWITCH_COMPONENT_ID,
    SWITCH_SUBENTRY_ID,
    SWITCH_VAR,
)

pytestmark = pytest.mark.usefixtures("mock_setup_entry")

SWITCH_INPUT = {"name": "Pump", "adsvar": SWITCH_VAR}


CONNECT_ERRORS = [
    pytest.param(
        lambda mock: setattr(
            mock.return_value.read_state,
            "side_effect",
            pyads.ADSError(err_code=1861),
        ),
        "cannot_connect",
        id="timeout",
    ),
    pytest.param(
        lambda mock: setattr(mock.return_value.read_state, "return_value", None),
        "cannot_connect",
        id="port_closed",
    ),
    pytest.param(
        lambda mock: setattr(
            mock.return_value.open,
            "side_effect",
            RuntimeError("Failed to open port on AMS router."),
        ),
        "cannot_connect",
        id="router_down",
    ),
    pytest.param(
        lambda mock: setattr(mock.return_value.read_state, "side_effect", TypeError),
        "unknown",
        id="unknown",
    ),
]


async def test_user_flow(hass: HomeAssistant, mock_pyads_connection: MagicMock) -> None:
    """Test the user flow creates an entry once the device answers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ADS_CONFIG
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == AMS_NET_ID
    assert result["data"] == ADS_CONFIG
    mock_pyads_connection.assert_called_once_with(AMS_NET_ID, PORT, IP_ADDRESS)
    client = mock_pyads_connection.return_value
    client.read_state.assert_called_once()
    # The probe does not keep the connection open.
    client.close.assert_called_once()


@pytest.mark.parametrize(("configure_mock", "error"), CONNECT_ERRORS)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    configure_mock: Callable[[MagicMock], None],
    error: str,
) -> None:
    """Test the user flow shows an error and recovers once the device answers."""
    configure_mock(mock_pyads_connection)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ADS_CONFIG
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_pyads_connection.side_effect = None
    client = mock_pyads_connection.return_value
    client.open.side_effect = None
    client.read_state.side_effect = None
    client.read_state.return_value = (5, 0)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ADS_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ADS_CONFIG


@pytest.mark.parametrize(
    "net_id", ["abc", "1.2.3", "1.2.3.4.5.256", "1.2.3.4.5.²", "1.2.3.4.5.0001"]
)
async def test_user_flow_invalid_net_id(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_create_connection: MagicMock,
    net_id: str,
) -> None:
    """Test a malformed AMS NetID is reported on its field without connecting."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: net_id, CONF_PORT: PORT}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DEVICE: "invalid_net_id"}
    mock_create_connection.assert_not_called()
    mock_pyads_connection.assert_not_called()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ADS_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unreachable(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_create_connection: MagicMock,
) -> None:
    """Test an unreachable device is reported at once, without pyads."""
    mock_create_connection.side_effect = TimeoutError("timed out")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ADS_CONFIG
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    mock_pyads_connection.assert_not_called()

    mock_create_connection.side_effect = None
    mock_pyads_connection.return_value.read_state.return_value = (5, 0)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ADS_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("mock_pyads_connection")
async def test_user_flow_single_instance(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test only a single connection can be configured."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.usefixtures("mock_pyads_connection")
async def test_import_flow(hass: HomeAssistant) -> None:
    """Test importing the connection from YAML."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=ADS_CONFIG
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == AMS_NET_ID
    assert result["data"] == ADS_CONFIG


@pytest.mark.parametrize(
    "configure_mock",
    [pytest.param(param.values[0], id=param.id) for param in CONNECT_ERRORS],
)
async def test_import_flow_offline(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    configure_mock: Callable[[MagicMock], None],
) -> None:
    """Test the import creates the entry without connecting to the device."""
    configure_mock(mock_pyads_connection)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=ADS_CONFIG
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ADS_CONFIG
    mock_pyads_connection.assert_not_called()


@pytest.mark.parametrize(
    "net_id",
    [
        "bad",
        "1.2.3.4.5",
        "1.2.3.4.5.256",
        "1.2.3.4.5.x",
        "1.2.3.4.5.²",
        "1.2.3.4.5.6.7",
    ],
)
async def test_import_flow_invalid_net_id(hass: HomeAssistant, net_id: str) -> None:
    """Test the import aborts on an AMS NetID that cannot be valid."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={**ADS_CONFIG, CONF_DEVICE: net_id},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_net_id"
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.usefixtures("mock_pyads_connection")
async def test_reconfigure_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test reconfiguring the connection, prefilled with the current settings."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    schema = result["data_schema"].schema
    assert {
        key: key.description["suggested_value"] for key in schema if key.description
    } == ADS_CONFIG

    new_net_id = "192.168.1.20.1.1"
    new_config = {**ADS_CONFIG, CONF_DEVICE: new_net_id, CONF_PORT: 852}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_config
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == new_config
    assert mock_config_entry.title == new_net_id


async def test_reconfigure_flow_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_pyads_connection: MagicMock,
) -> None:
    """Test a reconfiguration the device does not answer is retried, not stored."""
    mock_config_entry.add_to_hass(hass)
    mock_pyads_connection.return_value.read_state.side_effect = pyads.ADSError(
        err_code=1861
    )

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_PORT: 852}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert mock_config_entry.data == ADS_CONFIG

    mock_pyads_connection.return_value.read_state.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_PORT: 852}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {**ADS_CONFIG, CONF_PORT: 852}


async def test_reconfigure_flow_clears_optional_fields(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_pyads_connection: MagicMock,
) -> None:
    """Test a cleared IP address is dropped instead of stored as a blank."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_IP_ADDRESS: ""}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {CONF_DEVICE: AMS_NET_ID, CONF_PORT: PORT}
    # A blank address would be used as is instead of derived from the NetID.
    mock_pyads_connection.assert_called_once_with(AMS_NET_ID, PORT, None)


@pytest.mark.parametrize(
    ("change", "probed"),
    [
        pytest.param({CONF_IP_ADDRESS: "192.168.1.30"}, False, id="new_address"),
        pytest.param({CONF_PORT: 852}, True, id="new_port"),
    ],
)
async def test_reconfigure_loaded_entry(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_create_connection: MagicMock,
    change: dict[str, Any],
    probed: bool,
) -> None:
    """Test a new address for the running NetID is only checked for reachability.

    The AMS router keeps one route per NetID, so on Linux it refuses a second
    connection to the same NetID at another address while the hub holds the
    first. The reload connects to the new address instead.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=AMS_NET_ID,
        data=ADS_CONFIG,
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    new_config = {**ADS_CONFIG, **change}

    result = await entry.start_reconfigure_flow(hass)
    with patch("homeassistant.components.ads.async_unload_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], new_config
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == new_config
    assert mock_create_connection.call_args.args[0] == (
        new_config[CONF_IP_ADDRESS],
        48898,
    )
    assert mock_pyads_connection.called is probed


@pytest.fixture
def mock_hub() -> MagicMock:
    """Return a hub that reports the variable as a BOOL."""
    hub = MagicMock()
    hub.get_symbol_type.return_value = "BOOL"
    hub.resubscribe_task = None
    return hub


@pytest.fixture
def loaded_entry(hass: HomeAssistant, mock_hub: MagicMock) -> MockConfigEntry:
    """Return a loaded ADS entry backed by the mock hub."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=AMS_NET_ID,
        data=ADS_CONFIG,
        state=ConfigEntryState.LOADED,
    )
    entry.runtime_data = mock_hub
    entry.add_to_hass(hass)
    return entry


async def _async_start_switch_flow(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    """Start adding a device and pick a switch as its entity."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["switch", "gvl"]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "switch"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "switch"
    return result


def _component(subentry: Any) -> dict[str, Any]:
    """Return the only component of a device subentry."""
    (component,) = subentry.data["components"].values()
    return component


@pytest.mark.parametrize("symbol_type", ["BOOL", "BIT", "bool"])
async def test_switch_subentry_flow(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_hub: MagicMock,
    symbol_type: str,
) -> None:
    """Test a switch for a BOOL variable is created without asking."""
    mock_hub.get_symbol_type.return_value = symbol_type
    result = await _async_start_switch_flow(hass, loaded_entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = loaded_entry.subentries.values()
    assert subentry.subentry_type == "device"
    assert subentry.title == "Pump"
    assert _component(subentry) == {"platform": "switch", "adsvar": SWITCH_VAR}
    assert subentry.unique_id is None
    mock_hub.get_symbol_type.assert_called_once_with(SWITCH_VAR)


@pytest.mark.parametrize("symbol_type", ["INT", "REAL", "STRING(80)", "DATE_AND_TIME"])
async def test_switch_subentry_flow_wrong_type(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_hub: MagicMock,
    symbol_type: str,
) -> None:
    """Test a variable known not to be a BOOL is refused, then a BOOL accepted."""
    mock_hub.get_symbol_type.return_value = symbol_type
    result = await _async_start_switch_flow(hass, loaded_entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"adsvar": "wrong_type"}
    assert result["description_placeholders"] == {"type": symbol_type}

    mock_hub.get_symbol_type.return_value = "BOOL"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("configure_hub", "step_id", "placeholders"),
    [
        pytest.param(
            lambda hub: setattr(
                hub.get_symbol_type, "side_effect", pyads.ADSError(err_code=1808)
            ),
            "confirm_not_found",
            {"adsvar": SWITCH_VAR},
            id="not_found",
        ),
        pytest.param(
            lambda hub: setattr(
                hub.get_symbol_type, "side_effect", pyads.ADSError(err_code=1861)
            ),
            "confirm_cannot_connect",
            {"adsvar": SWITCH_VAR, "error": "ADSError: timeout elapsed (1861). "},
            id="timeout",
        ),
        pytest.param(
            lambda hub: setattr(
                hub.get_symbol_type,
                "side_effect",
                RuntimeError("Failed to open port on AMS router."),
            ),
            "confirm_cannot_connect",
            {"adsvar": SWITCH_VAR, "error": "Failed to open port on AMS router."},
            id="router_down",
        ),
        pytest.param(
            lambda hub: setattr(hub.get_symbol_type, "return_value", "T_Flag"),
            "confirm_unknown_type",
            {"adsvar": SWITCH_VAR, "type": "T_Flag"},
            id="alias",
        ),
    ],
)
async def test_switch_subentry_flow_unverified(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_hub: MagicMock,
    configure_hub: Callable[[MagicMock], None],
    step_id: str,
    placeholders: dict[str, str],
) -> None:
    """Test a variable that cannot be checked is saved once confirmed."""
    configure_hub(mock_hub)
    result = await _async_start_switch_flow(hass, loaded_entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    assert result["description_placeholders"] == placeholders

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = loaded_entry.subentries.values()
    assert subentry.title == "Pump"
    assert _component(subentry) == {"platform": "switch", "adsvar": SWITCH_VAR}


async def test_switch_subentry_flow_entry_not_loaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test a switch can be saved while the connection is not set up."""
    mock_config_entry.add_to_hass(hass)
    result = await _async_start_switch_flow(hass, mock_config_entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_not_loaded"

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(mock_config_entry.subentries) == 1


async def test_switch_subentry_flow_unknown_error(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_hub: MagicMock
) -> None:
    """Test an unexpected error is shown, and the flow recovers from it."""
    mock_hub.get_symbol_type.side_effect = TypeError
    result = await _async_start_switch_flow(hass, loaded_entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    mock_hub.get_symbol_type.side_effect = None
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SWITCH_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_switch_subentry_reconfigure(
    hass: HomeAssistant, mock_hub: MagicMock
) -> None:
    """Test reconfiguring a switch updates its subentry and reloads the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=AMS_NET_ID,
        data=ADS_CONFIG,
        state=ConfigEntryState.LOADED,
        subentries_data=[
            ConfigSubentryDataWithId(
                subentry_id=SWITCH_SUBENTRY_ID,
                subentry_type="device",
                title="Pump",
                data={
                    "components": {
                        SWITCH_COMPONENT_ID: {
                            "platform": "switch",
                            "adsvar": SWITCH_VAR,
                        }
                    }
                },
                unique_id=None,
            )
        ],
    )
    entry.runtime_data = mock_hub
    entry.add_to_hass(hass)

    result = await entry.start_subentry_reconfigure_flow(hass, SWITCH_SUBENTRY_ID)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "switch"
    schema = result["data_schema"].schema
    assert {key: key.description for key in schema} == {
        "name": {"suggested_value": "Pump"},
        "adsvar": {"suggested_value": SWITCH_VAR},
    }

    with patch.object(hass.config_entries, "async_schedule_reload") as mock_reload:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"name": "Fan", "adsvar": "GVL.bFan"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    subentry = entry.subentries[SWITCH_SUBENTRY_ID]
    assert subentry.title == "Fan"
    # The component keeps its ID, so the entity keeps its unique ID.
    assert subentry.data == {
        "components": {
            SWITCH_COMPONENT_ID: {"platform": "switch", "adsvar": "GVL.bFan"}
        }
    }
    mock_reload.assert_called_once_with(entry.entry_id)
    mock_hub.get_symbol_type.assert_called_once_with("GVL.bFan")
