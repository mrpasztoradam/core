"""Test the ADS hub."""

from collections.abc import AsyncGenerator
from datetime import timedelta
import struct
from typing import Any
from unittest.mock import MagicMock

import pyads
import pytest

from homeassistant.components.ads.hub import (
    KEEPALIVE_INTERVAL,
    NOTIFICATION_CYCLE_TIME,
    NOTIFICATION_MAX_DELAY,
    RECONNECT_MIN_INTERVAL,
    AdsHub,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import build_notification

from tests.common import async_fire_time_changed


@pytest.fixture
def ads_client() -> MagicMock:
    """Return a mocked pyads client."""
    return MagicMock()


@pytest.fixture
async def hub(hass: HomeAssistant, ads_client: MagicMock) -> AsyncGenerator[AdsHub]:
    """Return an AdsHub connected to the mocked client."""
    hub = AdsHub(hass, ads_client)
    await hub.async_setup()
    yield hub
    await hub.async_shutdown()


async def drop_connection(hass: HomeAssistant, ads_client: MagicMock) -> None:
    """Let the next keepalive probe find the device gone."""
    ads_client.read_state.side_effect = pyads.ADSError(text="timeout")
    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)


async def restore_connection(hass: HomeAssistant, ads_client: MagicMock) -> None:
    """Let the device answer again, and run the pending reconnect attempt."""
    ads_client.read_state.side_effect = None
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_confirms_the_device_answers(
    hass: HomeAssistant, ads_client: MagicMock
) -> None:
    """Test setting up opens the connection and makes a request on it."""
    hub = AdsHub(hass, ads_client)

    await hub.async_setup()

    ads_client.open.assert_called_once()
    ads_client.read_state.assert_called_once()
    assert hub.connected

    await hub.async_shutdown()


async def test_setup_closes_a_connection_that_stays_silent(
    hass: HomeAssistant, ads_client: MagicMock
) -> None:
    """Test a port the device never answers on is not left open.

    On Linux, open() only adds a route, so a device that has no route back to
    this client refuses the first request rather than the connection.
    """
    ads_client.read_state.side_effect = pyads.ADSError(text="timeout")
    hub = AdsHub(hass, ads_client)

    with pytest.raises(pyads.ADSError):
        await hub.async_setup()

    ads_client.close.assert_called_once()
    assert not hub.connected


async def test_write_by_name(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test writing a value by name."""
    hub.write_by_name("GVL.test", 42, pyads.PLCTYPE_INT)

    ads_client.write_by_name.assert_called_once_with("GVL.test", 42, pyads.PLCTYPE_INT)


async def test_read_by_name(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test reading a value by name."""
    ads_client.read_by_name.return_value = 42

    assert hub.read_by_name("GVL.test", pyads.PLCTYPE_INT) == 42


@pytest.mark.parametrize(
    ("method", "args"),
    [
        pytest.param("write_by_name", ("GVL.test", 42, pyads.PLCTYPE_INT), id="write"),
        pytest.param("read_by_name", ("GVL.test", pyads.PLCTYPE_INT), id="read"),
    ],
)
async def test_io_error_is_logged(
    hub: AdsHub, ads_client: MagicMock, method: str, args: tuple
) -> None:
    """Test an I/O error is logged instead of raised."""
    getattr(ads_client, method).side_effect = pyads.ADSError(text="timeout")

    assert getattr(hub, method)(*args) is None


@pytest.mark.parametrize(
    ("method", "args"),
    [
        pytest.param("write_by_name", ("GVL.test", 42, pyads.PLCTYPE_INT), id="write"),
        pytest.param("read_by_name", ("GVL.test", pyads.PLCTYPE_INT), id="read"),
    ],
)
async def test_io_after_shutdown(
    hub: AdsHub, ads_client: MagicMock, method: str, args: tuple
) -> None:
    """Test I/O is refused once the hub is shut down."""
    await hub.async_shutdown()

    assert getattr(hub, method)(*args) is None

    getattr(ads_client, method).assert_not_called()


async def test_subscribe(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test subscribing registers a notification and returns its id."""
    ads_client.add_device_notification.return_value = (1, 2)

    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    assert subscription is not None
    assert ads_client.add_device_notification.call_args.args[0] == "GVL.test"


async def test_subscribe_error_is_logged(
    hub: AdsHub, ads_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a notification subscription error is logged instead of raised."""
    ads_client.add_device_notification.side_effect = pyads.ADSError(text="timeout")

    assert hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock()) is not None

    assert "Error subscribing to GVL.test" in caplog.text


async def test_subscribe_closed_connection(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test subscribing on a closed connection leaves no handle behind.

    pyads returns None instead of raising once the port is gone.
    """
    ads_client.add_device_notification.return_value = None

    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    assert subscription is not None
    hub.unsubscribe(subscription)
    ads_client.del_device_notification.assert_not_called()


async def test_subscribe_after_shutdown(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test a late subscription is refused once the hub is shut down."""
    await hub.async_shutdown()

    assert hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock()) is None

    ads_client.add_device_notification.assert_not_called()


async def test_unsubscribe(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test dropping a subscription deletes its notification."""
    ads_client.add_device_notification.return_value = (1, 2)
    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    hub.unsubscribe(subscription)

    ads_client.del_device_notification.assert_called_once_with(1, 2)


async def test_unsubscribe_unknown_id(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test dropping a subscription that shutdown already tore down."""
    hub.unsubscribe(1)

    ads_client.del_device_notification.assert_not_called()


async def test_unsubscribe_error_is_logged(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test a deletion error is logged instead of raised."""
    ads_client.add_device_notification.return_value = (1, 2)
    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())
    ads_client.del_device_notification.side_effect = pyads.ADSError(text="timeout")

    hub.unsubscribe(subscription)

    ads_client.del_device_notification.assert_called_once_with(1, 2)


async def test_shutdown(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test shutdown deletes notifications and closes the connection."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    await hub.async_shutdown()

    ads_client.del_device_notification.assert_called_once_with(1, 2)
    ads_client.close.assert_called_once()


async def test_shutdown_ignores_ads_errors(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test shutdown still closes the connection if cleanup calls fail."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())
    ads_client.del_device_notification.side_effect = pyads.ADSError(text="timeout")
    ads_client.close.side_effect = pyads.ADSError(text="timeout")

    await hub.async_shutdown()

    ads_client.close.assert_called_once()


async def test_shutdown_stops_probing(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the connection is left alone once the hub is shut down."""
    await hub.async_shutdown()
    ads_client.read_state.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    ads_client.read_state.assert_not_called()


async def test_a_healthy_connection_is_left_alone(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that keeps answering is not reconnected to."""
    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hub.connected
    # The probe is the one request on top of the one setup made.
    assert ads_client.read_state.call_count == 2
    ads_client.close.assert_not_called()


async def test_connection_loss_is_announced(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that stops answering is noticed and reported."""
    listener = MagicMock()
    hub.async_add_connection_listener(listener)

    await drop_connection(hass, ads_client)

    assert not hub.connected
    listener.assert_called_once()
    # Closing a connection the device has dropped crashes the ADS library
    # and takes the process with it, so the drop leaves it alone.
    ads_client.close.assert_not_called()


async def test_shutdown_leaves_a_dropped_connection_alone(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a connection the device already dropped is not closed on the way out."""
    await drop_connection(hass, ads_client)

    await hub.async_shutdown()

    ads_client.close.assert_not_called()


async def test_connection_listener_can_be_dropped(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a removed entity is not called back on."""
    listener = MagicMock()
    hub.async_add_connection_listener(listener)()

    await drop_connection(hass, ads_client)

    listener.assert_not_called()


async def test_reconnect_resubscribes(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test notifications are registered again on the rebuilt connection.

    The device reissues the handles, so the ones held before the drop are no
    use for routing what arrives after it.
    """
    ads_client.add_device_notification.return_value = (1, 2)
    notification_callback = MagicMock()
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, notification_callback)

    await drop_connection(hass, ads_client)
    ads_client.add_device_notification.return_value = (7, 8)
    await restore_connection(hass, ads_client)

    assert hub.connected
    ads_client.open.assert_called_once()
    handler = ads_client.add_device_notification.call_args.args[2]
    handler(build_notification(7, b"\x01"), "GVL.test")

    notification_callback.assert_called_once_with("GVL.test", True)


async def test_subscription_id_survives_a_reconnect(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test an entity can still drop a subscription it made before a drop."""
    ads_client.add_device_notification.return_value = (1, 2)
    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())

    await drop_connection(hass, ads_client)
    ads_client.add_device_notification.return_value = (7, 8)
    await restore_connection(hass, ads_client)
    hub.unsubscribe(subscription)

    ads_client.del_device_notification.assert_called_once_with(7, 8)


async def test_subscribing_while_disconnected_is_deferred(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test an entity added while the device is gone is picked up later."""
    await drop_connection(hass, ads_client)
    ads_client.add_device_notification.reset_mock()

    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())

    ads_client.add_device_notification.assert_not_called()

    ads_client.add_device_notification.return_value = (1, 2)
    await restore_connection(hass, ads_client)

    assert ads_client.add_device_notification.call_args.args[0] == "GVL.test"


async def test_reconnect_backs_off(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that stays away is not asked again every few seconds."""
    await drop_connection(hass, ads_client)
    ads_client.read_state.reset_mock()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ads_client.read_state.call_count == 1

    # The failed attempt pushed the next one out past the minimum interval.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ads_client.read_state.call_count == 1

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL * 2 + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ads_client.read_state.call_count == 2


@pytest.mark.parametrize(
    ("plc_datatype", "payload", "expected"),
    [
        pytest.param(pyads.PLCTYPE_BOOL, b"\x01", True, id="bool"),
        pytest.param(pyads.PLCTYPE_INT, struct.pack("<h", -42), -42, id="int"),
        pytest.param(pyads.PLCTYPE_BYTE, struct.pack("<B", 200), 200, id="byte"),
        pytest.param(
            pyads.PLCTYPE_DT, struct.pack("<I", 2**31 + 1), 2**31 + 1, id="dt"
        ),
        pytest.param(pyads.PLCTYPE_TOD, struct.pack("<i", -1), -1, id="tod"),
        pytest.param(pyads.PLCTYPE_REAL, struct.pack("<f", 1.5), 1.5, id="real"),
        pytest.param(pyads.PLCTYPE_STRING, b"hello\x00rest", "hello", id="string"),
        pytest.param(
            pyads.PLCTYPE_LINT, b"\x01\x02", bytearray(b"\x01\x02"), id="unsupported"
        ),
    ],
)
async def test_notification_is_decoded(
    hub: AdsHub,
    ads_client: MagicMock,
    plc_datatype: type,
    payload: bytes,
    expected: Any,
) -> None:
    """Test an incoming notification is decoded for its PLC data type."""
    ads_client.add_device_notification.return_value = (1, 2)
    notification_callback = MagicMock()
    hub.subscribe("GVL.test", plc_datatype, notification_callback)

    # The router calls back into the handler the hub subscribed with.
    handler = ads_client.add_device_notification.call_args.args[2]
    handler(build_notification(1, payload), "GVL.test")

    notification_callback.assert_called_once_with("GVL.test", expected)


async def test_notification_for_unknown_handle(
    hub: AdsHub, ads_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a notification for a handle the hub does not know is logged."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())
    handler = ads_client.add_device_notification.call_args.args[2]

    handler(build_notification(99, b"\x01"), "GVL.test")

    assert "Unknown device notification handle: 99" in caplog.text


async def test_notification_timing_is_tuned(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test subscriptions do not ask for pyads' 100 ns defaults."""
    ads_client.add_device_notification.return_value = (1, 2)

    hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    attr = ads_client.add_device_notification.call_args.args[1]
    # pyads takes milliseconds but reports back 100 ns ticks.
    assert attr.cycle_time == NOTIFICATION_CYCLE_TIME * 1e4
    assert attr.max_delay == NOTIFICATION_MAX_DELAY * 1e4
