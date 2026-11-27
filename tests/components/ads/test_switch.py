"""Test the ADS switch platform."""

from typing import Any
from unittest.mock import MagicMock, call, patch

import pyads
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.ads.const import DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SwitchService
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, TargetKey
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import MockAdsDevice, async_run_past_first_update, setup_ads_platform
from .const import SWITCH_COMPONENT_ID, SWITCH_SUBENTRY_ID, SWITCH_VAR

ADS_VAR = "GVL.pump"
ENTITY_ID = "switch.pump"
PLATFORM_CONFIG = {"adsvar": ADS_VAR, "name": "Pump"}

UI_ENTITY_ID = "switch.pump"

BOOL_TRUE = b"\x01"
BOOL_FALSE = b"\x00"


@pytest.mark.parametrize(
    ("platform_config", "entity_id"),
    [
        pytest.param(PLATFORM_CONFIG, ENTITY_ID, id="named"),
        pytest.param({"adsvar": ADS_VAR}, "switch.ads_switch", id="defaults"),
    ],
)
async def test_switch(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_ads_device: MockAdsDevice,
    snapshot: SnapshotAssertion,
    platform_config: dict[str, Any],
    entity_id: str,
) -> None:
    """Test the switch is created from YAML with the PLC's value."""
    mock_ads_device.values[ADS_VAR] = BOOL_TRUE

    assert await setup_ads_platform(
        hass, mock_ads_device, SWITCH_DOMAIN, platform_config
    )

    assert entity_registry.async_get(entity_id) == snapshot(name="entry")
    assert hass.states.get(entity_id) == snapshot(name="state")


async def test_state_follows_the_plc(
    hass: HomeAssistant, mock_ads_device: MockAdsDevice
) -> None:
    """Test the switch follows the values the PLC pushes."""
    mock_ads_device.values[ADS_VAR] = BOOL_TRUE
    assert await setup_ads_platform(
        hass, mock_ads_device, SWITCH_DOMAIN, PLATFORM_CONFIG
    )
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    await mock_ads_device.async_push(hass, ADS_VAR, BOOL_FALSE)
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


@pytest.mark.parametrize(
    ("initial", "service", "written"),
    [
        pytest.param(BOOL_FALSE, SwitchService.TURN_ON, True, id="turn_on"),
        pytest.param(BOOL_TRUE, SwitchService.TURN_OFF, False, id="turn_off"),
    ],
)
async def test_actions_write_to_the_plc(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
    initial: bytes,
    service: str,
    written: bool,
) -> None:
    """Test the actions write the variable, leaving the state to the PLC."""
    mock_ads_device.values[ADS_VAR] = initial
    assert await setup_ads_platform(
        hass, mock_ads_device, SWITCH_DOMAIN, PLATFORM_CONFIG
    )
    state = hass.states.get(ENTITY_ID).state

    await hass.services.async_call(
        SWITCH_DOMAIN, service, {TargetKey.ENTITY_ID: ENTITY_ID}, blocking=True
    )

    mock_pyads_connection.return_value.write_by_name.assert_called_once_with(
        ADS_VAR, written, pyads.PLCTYPE_BOOL
    )
    # Nothing changes until the PLC pushes the new value back.
    assert hass.states.get(ENTITY_ID).state == state


async def test_write_error(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failing write is logged instead of raised at the caller."""
    mock_ads_device.values[ADS_VAR] = BOOL_FALSE
    assert await setup_ads_platform(
        hass, mock_ads_device, SWITCH_DOMAIN, PLATFORM_CONFIG
    )
    mock_pyads_connection.return_value.write_by_name.side_effect = pyads.ADSError(
        err_code=1861
    )

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SwitchService.TURN_ON,
        {TargetKey.ENTITY_ID: ENTITY_ID},
        blocking=True,
    )

    assert f"Error writing {ADS_VAR}: ADSError: timeout elapsed (1861)" in caplog.text
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def _async_setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, device: MockAdsDevice
) -> None:
    """Set up the entry and wait for the first values of its switches."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for delivery in device.deliveries:
        await hass.async_add_executor_job(delivery.join)
    await hass.async_block_till_done()


async def test_ui_switch(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_ads_device: MockAdsDevice,
    snapshot: SnapshotAssertion,
) -> None:
    """Test a switch subentry gets its own device under the PLC's."""
    mock_ads_device.values[SWITCH_VAR] = BOOL_TRUE

    await _async_setup_entry(hass, mock_config_entry_with_switch, mock_ads_device)

    await snapshot_platform(
        hass, entity_registry, snapshot, mock_config_entry_with_switch.entry_id
    )
    entity = entity_registry.async_get(UI_ENTITY_ID)
    assert entity.unique_id == f"{SWITCH_SUBENTRY_ID}_{SWITCH_COMPONENT_ID}"
    assert entity.config_subentry_id == SWITCH_SUBENTRY_ID
    device = device_registry.async_get(entity.device_id)
    assert device.identifiers == {(DOMAIN, SWITCH_SUBENTRY_ID)}
    assert device.name == "Pump"
    assert device.config_entry_id == mock_config_entry_with_switch.entry_id
    assert device.config_subentry_id == SWITCH_SUBENTRY_ID
    hub_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, mock_config_entry_with_switch.entry_id),
        mock_config_entry_with_switch.entry_id,
    )
    assert device.via_device_id == hub_device.id


async def test_ui_switch_writes_to_the_plc(
    hass: HomeAssistant,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test a UI switch writes its variable and follows the PLC."""
    mock_ads_device.values[SWITCH_VAR] = BOOL_FALSE
    await _async_setup_entry(hass, mock_config_entry_with_switch, mock_ads_device)

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SwitchService.TURN_ON,
        {TargetKey.ENTITY_ID: UI_ENTITY_ID},
        blocking=True,
    )

    mock_pyads_connection.return_value.write_by_name.assert_called_once_with(
        SWITCH_VAR, True, pyads.PLCTYPE_BOOL
    )
    await mock_ads_device.async_push(hass, SWITCH_VAR, BOOL_TRUE)
    assert hass.states.get(UI_ENTITY_ID).state == STATE_ON


async def test_ui_switch_added_without_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test a switch added to a loaded entry appears without reloading it."""
    mock_ads_device.values[SWITCH_VAR] = BOOL_TRUE
    await _async_setup_entry(hass, mock_config_entry, mock_ads_device)
    assert hass.states.get(UI_ENTITY_ID) is None

    with patch("homeassistant.components.ads.async_setup_entry") as mock_setup:
        hass.config_entries.async_add_subentry(
            mock_config_entry,
            ConfigSubentry(
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
            ),
        )
        await hass.async_block_till_done()
        for delivery in mock_ads_device.deliveries:
            await hass.async_add_executor_job(delivery.join)
        await hass.async_block_till_done()

    mock_setup.assert_not_called()
    assert hass.states.get(UI_ENTITY_ID).state == STATE_ON


async def test_ui_switch_removed(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test deleting a switch subentry removes it and its notification."""
    mock_ads_device.values[SWITCH_VAR] = BOOL_TRUE
    await _async_setup_entry(hass, mock_config_entry_with_switch, mock_ads_device)
    client = mock_pyads_connection.return_value

    hass.config_entries.async_remove_subentry(
        mock_config_entry_with_switch, SWITCH_SUBENTRY_ID
    )
    await hass.async_block_till_done()

    assert hass.states.get(UI_ENTITY_ID) is None
    assert entity_registry.async_get(UI_ENTITY_ID) is None
    assert (
        device_registry.async_get_device_by_identifier(
            (DOMAIN, SWITCH_SUBENTRY_ID), mock_config_entry_with_switch.entry_id
        )
        is None
    )
    client.del_device_notification.assert_called_once_with(1, 1)
    assert mock_config_entry_with_switch.state is ConfigEntryState.LOADED


async def test_ui_switch_reconfigured(
    hass: HomeAssistant,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test a reconfigured switch keeps its entity ID and reads the new variable."""
    mock_ads_device.values[SWITCH_VAR] = BOOL_TRUE
    mock_ads_device.values["GVL.bFan"] = BOOL_FALSE
    await _async_setup_entry(hass, mock_config_entry_with_switch, mock_ads_device)

    result = await mock_config_entry_with_switch.start_subentry_reconfigure_flow(
        hass, SWITCH_SUBENTRY_ID
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Fan", "adsvar": "GVL.bFan"}
    )
    await hass.async_block_till_done()
    for delivery in mock_ads_device.deliveries:
        await hass.async_add_executor_job(delivery.join)
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert hass.states.get(UI_ENTITY_ID).state == STATE_OFF
    mock_pyads_connection.return_value.get_symbol.assert_called_once_with("GVL.bFan")


async def test_ui_switch_unloaded_before_the_connection_closes(
    hass: HomeAssistant,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test unloading the entry deletes the switch's notification, then closes."""
    mock_ads_device.values[SWITCH_VAR] = BOOL_TRUE
    await _async_setup_entry(hass, mock_config_entry_with_switch, mock_ads_device)
    client = mock_pyads_connection.return_value

    await hass.config_entries.async_unload(mock_config_entry_with_switch.entry_id)
    await hass.async_block_till_done()

    assert client.mock_calls.index(call.del_device_notification(1, 1)) < (
        client.mock_calls.index(call.close())
    )
    assert hass.states.get(UI_ENTITY_ID).state == STATE_UNAVAILABLE


async def test_ui_switch_set_up_without_waiting(
    hass: HomeAssistant,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test the entry loads before a UI switch's first value, which it reads late.

    The PLC's first notification can be lost, so the switch reads the
    variable once when no first value arrived in time.
    """
    mock_pyads_connection.return_value.read_by_name.return_value = True
    mock_config_entry_with_switch.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry_with_switch.entry_id)
    assert mock_config_entry_with_switch.state is ConfigEntryState.LOADED
    assert hass.states.get(UI_ENTITY_ID).state == STATE_UNAVAILABLE

    await async_run_past_first_update(
        hass, mock_ads_device, hass.async_block_till_done()
    )

    mock_pyads_connection.return_value.read_by_name.assert_called_once_with(
        SWITCH_VAR, pyads.PLCTYPE_BOOL
    )
    assert hass.states.get(UI_ENTITY_ID).state == STATE_ON


async def test_ui_switch_removed_before_its_first_value(
    hass: HomeAssistant,
    mock_config_entry_with_switch: MockConfigEntry,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test deleting a switch still waiting for its first value leaves no state."""
    mock_pyads_connection.return_value.read_by_name.return_value = True
    mock_config_entry_with_switch.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry_with_switch.entry_id)
    assert hass.states.get(UI_ENTITY_ID) is not None

    hass.config_entries.async_remove_subentry(
        mock_config_entry_with_switch, SWITCH_SUBENTRY_ID
    )
    await async_run_past_first_update(
        hass, mock_ads_device, hass.async_block_till_done()
    )

    assert hass.states.get(UI_ENTITY_ID) is None
    # Nothing is read for a switch that is gone.
    mock_pyads_connection.return_value.read_by_name.assert_not_called()


async def test_yaml_switch_reads_a_lost_first_value(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    mock_ads_device: MockAdsDevice,
) -> None:
    """Test a YAML switch whose first notification is lost reads the variable."""
    mock_pyads_connection.return_value.read_by_name.return_value = True

    assert await async_run_past_first_update(
        hass,
        mock_ads_device,
        setup_ads_platform(hass, mock_ads_device, SWITCH_DOMAIN, PLATFORM_CONFIG),
    )

    assert hass.states.get(ENTITY_ID).state == STATE_ON
