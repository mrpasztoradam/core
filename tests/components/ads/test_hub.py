"""Test the ADS hub's local AMS NetID handling and connection helpers."""

from collections.abc import Callable
import threading
from unittest.mock import MagicMock

import pyads
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.ads.hub import (
    AdsHub,
    apply_local_net_id,
    async_get_hub,
    connect,
    local_net_id_probe,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady

from .conftest import MockPyadsLocalNetId
from .const import AMS_NET_ID, AUTO_NET_ID, IP_ADDRESS, LOCAL_NET_ID, PORT


async def test_async_get_hub_not_loaded(hass: HomeAssistant) -> None:
    """Test a platform set up before the connection is told to retry."""
    with pytest.raises(PlatformNotReady):
        async_get_hub(hass)


@pytest.mark.usefixtures("mock_pyads_connection")
async def test_async_get_hub(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test the hub of the loaded entry is returned."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert async_get_hub(hass) is mock_config_entry.runtime_data


def test_apply_local_net_id_without_override(
    mock_pyads_local_net_id: MockPyadsLocalNetId,
) -> None:
    """Test clearing a local AMS NetID that was never set leaves pyads alone."""
    apply_local_net_id(None)

    mock_pyads_local_net_id.open_port.assert_not_called()
    mock_pyads_local_net_id.set_local_address.assert_not_called()


def test_apply_local_net_id_and_restore(
    mock_pyads_local_net_id: MockPyadsLocalNetId,
) -> None:
    """Test a local AMS NetID is applied, then the generated one restored."""
    apply_local_net_id(LOCAL_NET_ID)
    mock_pyads_local_net_id.set_local_address.assert_called_once_with(LOCAL_NET_ID)

    apply_local_net_id(None)
    mock_pyads_local_net_id.set_local_address.assert_called_with(AUTO_NET_ID)
    # The router port is only opened around each call.
    assert (
        mock_pyads_local_net_id.open_port.call_count
        == mock_pyads_local_net_id.close_port.call_count
    )


def test_probe_without_local_net_id(
    mock_pyads_local_net_id: MockPyadsLocalNetId,
) -> None:
    """Test probing without a local AMS NetID never touches the process identity."""
    with local_net_id_probe(None):
        pass

    mock_pyads_local_net_id.set_local_address.assert_not_called()


def test_probe_blocks_hub_io(mock_pyads_local_net_id: MockPyadsLocalNetId) -> None:
    """Test the hub sends nothing while a probe has another local AMS NetID active."""
    client = MagicMock()
    hub = AdsHub(client)
    reading = threading.Event()

    def _read() -> None:
        reading.set()
        hub.read_by_name("GVL.setpoint", pyads.PLCTYPE_INT)

    reader = threading.Thread(target=_read)
    with local_net_id_probe(LOCAL_NET_ID):
        reader.start()
        reading.wait()
        reader.join(timeout=0.1)

        assert reader.is_alive()
        client.read_by_name.assert_not_called()

    reader.join()
    client.read_by_name.assert_called_once()


@pytest.mark.parametrize(
    "teardown",
    [
        pytest.param(lambda hub: hub.shutdown(), id="shutdown"),
        pytest.param(lambda hub: hub.delete_device_notification(1), id="removal"),
    ],
)
def test_probe_blocks_teardown(
    mock_pyads_local_net_id: MockPyadsLocalNetId,
    teardown: Callable[[AdsHub], None],
) -> None:
    """Test notifications are not deleted while a probe has another NetID active."""
    client = MagicMock()
    client.add_device_notification.return_value = (1, 1)
    hub = AdsHub(client)
    hub.add_device_notification("GVL.setpoint", pyads.PLCTYPE_INT, MagicMock())
    tearing_down = threading.Event()

    def _teardown() -> None:
        tearing_down.set()
        teardown(hub)

    worker = threading.Thread(target=_teardown)
    with local_net_id_probe(LOCAL_NET_ID):
        worker.start()
        tearing_down.wait()
        worker.join(timeout=0.1)

        assert worker.is_alive()
        client.del_device_notification.assert_not_called()

    worker.join()
    client.del_device_notification.assert_called_once_with(1, 1)


@pytest.mark.parametrize(
    "configure_mock",
    [
        pytest.param(
            lambda mock: setattr(
                mock.return_value.read_state,
                "side_effect",
                pyads.ADSError(err_code=1861),
            ),
            id="timeout",
        ),
        pytest.param(
            lambda mock: setattr(mock.return_value.read_state, "return_value", None),
            id="port_closed",
        ),
    ],
)
def test_connect_closes_on_error(
    mock_pyads_connection: MagicMock, configure_mock
) -> None:
    """Test a connection the device does not answer on is closed again."""
    configure_mock(mock_pyads_connection)

    with pytest.raises(pyads.ADSError):
        connect(AMS_NET_ID, PORT, IP_ADDRESS, None)

    mock_pyads_connection.return_value.close.assert_called_once()
