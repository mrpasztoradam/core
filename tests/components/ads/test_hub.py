"""Test the ADS hub's connection helpers."""

import asyncio
from collections.abc import Callable
import threading
from unittest.mock import MagicMock

import pyads
import pytest

from homeassistant.components.ads.const import DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE, CONF_PORT, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from . import ADS_CONFIG, MockAdsDevice, get_hub, setup_ads_platform
from .const import DEVICE_NAME, DEVICE_VERSION, PORT


async def test_platform_waits_for_the_import(
    hass: HomeAssistant,
    mock_ads_device: MockAdsDevice,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a YAML platform waits for the imported entry instead of retrying."""
    mock_ads_device.values["GVL.pump"] = b"\x01"

    assert await setup_ads_platform(
        hass,
        mock_ads_device,
        SWITCH_DOMAIN,
        {"adsvar": "GVL.pump", "name": "Pump"},
    )

    assert hass.states.get("switch.pump").state == STATE_ON
    assert "not ready yet" not in caplog.text


async def test_platform_gives_up_without_an_entry(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a YAML platform retries on its own when no entry ever sets up.

    A malformed NetID aborts the import, so the platform does not wait for it.
    """
    async with asyncio.timeout(5):
        assert await async_setup_component(
            hass,
            SWITCH_DOMAIN,
            {
                DOMAIN: {CONF_DEVICE: "bad", CONF_PORT: PORT},
                SWITCH_DOMAIN: {
                    "platform": DOMAIN,
                    "adsvar": "GVL.pump",
                    "name": "Pump",
                },
            },
        )
        await hass.async_block_till_done()

    assert hass.states.get("switch.pump") is None
    assert "The ADS connection is not set up" in caplog.text


async def test_platform_waits_for_a_retry_in_progress(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test a YAML platform set up during a retry waits for it, not the failure."""
    client = mock_pyads_connection.return_value
    client.read_state.side_effect = pyads.ADSError(err_code=1861)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    connecting, release = threading.Event(), threading.Event()

    def _slow_read_state() -> tuple[int, int]:
        connecting.set()
        release.wait(5)
        return (pyads.ADSSTATE_RUN, 0)

    client.read_state.side_effect = _slow_read_state
    mock_ads_device.values["GVL.pump"] = b"\x01"
    retry = hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    await hass.async_add_executor_job(connecting.wait, 5)
    platform = hass.async_create_task(
        async_setup_component(
            hass,
            SWITCH_DOMAIN,
            {SWITCH_DOMAIN: {"platform": DOMAIN, "adsvar": "GVL.pump", "name": "Pump"}},
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    await retry
    assert await platform
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("switch.pump").state == STATE_ON


async def test_get_symbol_type(
    hass: HomeAssistant, mock_pyads_connection: MagicMock, setup_ads: None
) -> None:
    """Test the hub returns the type the PLC reports for a variable."""
    client = mock_pyads_connection.return_value
    client.get_symbol.return_value.symbol_type = "INT"

    assert await hass.async_add_executor_job(
        get_hub(hass).get_symbol_type, "GVL.n"
    ) == ("INT")
    client.get_symbol.assert_called_once_with("GVL.n")


async def test_get_symbol_type_unknown_variable(
    hass: HomeAssistant, mock_pyads_connection: MagicMock, setup_ads: None
) -> None:
    """Test a variable the PLC does not know raises its ADS error."""
    mock_pyads_connection.return_value.get_symbol.side_effect = pyads.ADSError(
        err_code=1808
    )

    with pytest.raises(pyads.ADSError) as err:
        await hass.async_add_executor_job(get_hub(hass).get_symbol_type, "GVL.nope")
    assert err.value.err_code == 1808


async def test_get_symbol_type_after_shutdown(
    hass: HomeAssistant, mock_pyads_connection: MagicMock, setup_ads: None
) -> None:
    """Test the hub refuses to look up a variable once it is shut down."""
    hub = get_hub(hass)
    await hass.async_add_executor_job(hub.shutdown)

    with pytest.raises(pyads.ADSError) as err:
        await hass.async_add_executor_job(hub.get_symbol_type, "GVL.n")
    assert err.value.err_code == 1864
    mock_pyads_connection.return_value.get_symbol.assert_not_called()


async def test_read_device_info(hass: HomeAssistant, setup_ads: None) -> None:
    """Test the hub returns the device name and its version as text."""
    assert await hass.async_add_executor_job(get_hub(hass).read_device_info) == (
        DEVICE_NAME,
        DEVICE_VERSION,
    )


@pytest.mark.parametrize(
    "configure_mock",
    [
        pytest.param(
            lambda client: setattr(client.read_device_info, "return_value", None),
            id="port_closed",
        ),
        pytest.param(
            lambda client: setattr(
                client.read_device_info, "side_effect", pyads.ADSError(err_code=1861)
            ),
            id="timeout",
        ),
    ],
)
async def test_read_device_info_unanswered(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    setup_ads: None,
    configure_mock: Callable[[MagicMock], None],
) -> None:
    """Test the device info is None when the device does not answer."""
    configure_mock(mock_pyads_connection.return_value)

    assert await hass.async_add_executor_job(get_hub(hass).read_device_info) is None


async def test_read_device_info_after_shutdown(
    hass: HomeAssistant, mock_pyads_connection: MagicMock, setup_ads: None
) -> None:
    """Test the hub does not ask a closed connection for the device info."""
    hub = get_hub(hass)
    await hass.async_add_executor_job(hub.shutdown)
    client = mock_pyads_connection.return_value
    client.read_device_info.reset_mock()

    assert await hass.async_add_executor_job(hub.read_device_info) is None
    client.read_device_info.assert_not_called()
