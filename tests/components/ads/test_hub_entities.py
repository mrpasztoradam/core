"""Test the ADS entities reporting on the connection itself."""

from datetime import timedelta
from unittest.mock import MagicMock

import pyads
import pytest

from homeassistant.components.ads.const import DOMAIN
from homeassistant.components.ads.hub import KEEPALIVE_INTERVAL, RECONNECT_MIN_INTERVAL
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from . import ADS_CONFIG
from .const import AMS_NET_ID, PORT

from tests.common import async_fire_time_changed

CONNECTION_ENTITY_ID = "binary_sensor.ads_connection"
STATE_ENTITY_ID = "sensor.ads_state"


@pytest.fixture
async def ads_component(
    hass: HomeAssistant, mock_pyads_connection: MagicMock
) -> MagicMock:
    """Set up the ADS component and return the mocked client."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()
    return mock_pyads_connection.return_value


async def go_quiet(hass: HomeAssistant, client: MagicMock) -> None:
    """Let the next keepalive probe find the device gone."""
    client.read_state.side_effect = pyads.ADSError(text="timeout")
    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)


async def retry_reconnect(hass: HomeAssistant) -> None:
    """Run the reconnect attempt that is currently pending."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("ads_component")
async def test_entities_are_created_without_yaml(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Test configuring the connection is enough to get its diagnostics."""
    assert hass.states.get(CONNECTION_ENTITY_ID).state == STATE_ON
    assert hass.states.get(STATE_ENTITY_ID).state == "run"

    assert (
        entity_registry.async_get(CONNECTION_ENTITY_ID).unique_id
        == f"{AMS_NET_ID}:{PORT}-connection"
    )
    assert (
        entity_registry.async_get(STATE_ENTITY_ID).unique_id
        == f"{AMS_NET_ID}:{PORT}-ads_state"
    )


async def test_a_device_that_goes_quiet(
    hass: HomeAssistant, ads_component: MagicMock
) -> None:
    """Test an unreachable device is reported, not hidden behind unavailable."""
    await go_quiet(hass, ads_component)

    assert hass.states.get(CONNECTION_ENTITY_ID).state == STATE_OFF
    # Unknown rather than unavailable: the entity still works, the device is
    # simply no longer saying what it is doing.
    assert hass.states.get(STATE_ENTITY_ID).state == STATE_UNKNOWN


async def test_a_device_that_stops_running(
    hass: HomeAssistant, ads_component: MagicMock
) -> None:
    """Test a reachable device that is not in run is reported as such."""
    ads_component.read_state.return_value = (pyads.ADSSTATE_STOP, pyads.ADSSTATE_RUN)
    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(CONNECTION_ENTITY_ID).state == STATE_OFF
    assert hass.states.get(STATE_ENTITY_ID).state == "stop"


async def test_a_state_change_while_disconnected_is_reported(
    hass: HomeAssistant, ads_component: MagicMock
) -> None:
    """Test a device that comes back in config mode does not read as gone."""
    await go_quiet(hass, ads_component)

    ads_component.read_state.side_effect = None
    ads_component.read_state.return_value = (pyads.ADSSTATE_CONFIG, pyads.ADSSTATE_RUN)
    await retry_reconnect(hass)

    assert hass.states.get(CONNECTION_ENTITY_ID).state == STATE_OFF
    assert hass.states.get(STATE_ENTITY_ID).state == "config"


async def test_reconnecting_is_reported(
    hass: HomeAssistant, ads_component: MagicMock
) -> None:
    """Test the entities follow the device back up."""
    await go_quiet(hass, ads_component)
    ads_component.read_state.side_effect = None
    await retry_reconnect(hass)

    assert hass.states.get(CONNECTION_ENTITY_ID).state == STATE_ON
    assert hass.states.get(STATE_ENTITY_ID).state == "run"
