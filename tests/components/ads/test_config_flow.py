"""Test the ADS config flow."""

from collections.abc import Callable
from unittest.mock import MagicMock, call

import pyads
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.ads.const import CONF_LOCAL_NET_ID, DOMAIN
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.const import CONF_DEVICE, CONF_IP_ADDRESS, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from . import ADS_CONFIG
from .conftest import MockPyadsLocalNetId
from .const import AMS_NET_ID, AUTO_NET_ID, IP_ADDRESS, LOCAL_NET_ID, PORT

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


@pytest.fixture
def mock_validate_connection() -> None:
    """Run the real connection probe, against the mocked pyads Connection."""


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
            mock, "side_effect", ValueError("invalid literal for int() with base 10")
        ),
        "invalid_net_id",
        id="invalid_net_id",
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


async def test_user_flow_with_local_net_id(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_pyads_local_net_id: MockPyadsLocalNetId,
) -> None:
    """Test the probe runs with the local AMS NetID and restores the old one."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_LOCAL_NET_ID: LOCAL_NET_ID}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCAL_NET_ID] == LOCAL_NET_ID
    assert mock_pyads_local_net_id.set_local_address.call_args_list == [
        call(LOCAL_NET_ID),
        call(AUTO_NET_ID),
    ]


async def test_user_flow_local_net_id_restored_after_error(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_pyads_local_net_id: MockPyadsLocalNetId,
) -> None:
    """Test a failed probe still restores the previous local AMS NetID."""
    mock_pyads_connection.return_value.read_state.side_effect = pyads.ADSError(
        err_code=1861
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_LOCAL_NET_ID: LOCAL_NET_ID}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert mock_pyads_local_net_id.set_local_address.call_args_list == [
        call(LOCAL_NET_ID),
        call(AUTO_NET_ID),
    ]

    mock_pyads_connection.return_value.read_state.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_LOCAL_NET_ID: LOCAL_NET_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_invalid_local_net_id(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_pyads_local_net_id: MockPyadsLocalNetId,
) -> None:
    """Test a local AMS NetID pyads cannot parse is reported before connecting."""
    mock_pyads_local_net_id.set_local_address.side_effect = ValueError(
        "invalid literal for int() with base 10"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_LOCAL_NET_ID: "not-a-net-id"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_net_id"}
    mock_pyads_connection.assert_not_called()

    mock_pyads_local_net_id.set_local_address.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ADS_CONFIG, CONF_LOCAL_NET_ID: LOCAL_NET_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


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


@pytest.mark.parametrize(("configure_mock", "reason"), CONNECT_ERRORS)
async def test_import_flow_errors(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    configure_mock: Callable[[MagicMock], None],
    reason: str,
) -> None:
    """Test the import aborts when the device does not answer."""
    configure_mock(mock_pyads_connection)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=ADS_CONFIG
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
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


@pytest.mark.usefixtures("mock_pyads_local_net_id")
async def test_reconfigure_flow_clears_optional_fields(
    hass: HomeAssistant,
    mock_config_entry_local_net_id: MockConfigEntry,
    mock_pyads_connection: MagicMock,
) -> None:
    """Test cleared optional fields are dropped instead of stored as blanks."""
    mock_config_entry_local_net_id.add_to_hass(hass)

    result = await mock_config_entry_local_net_id.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**ADS_CONFIG, CONF_IP_ADDRESS: "", CONF_LOCAL_NET_ID: ""},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry_local_net_id.data == {
        CONF_DEVICE: AMS_NET_ID,
        CONF_PORT: PORT,
    }
    # A blank address would be used as is instead of derived from the NetID.
    mock_pyads_connection.assert_called_once_with(AMS_NET_ID, PORT, None)
