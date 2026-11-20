"""Tests for the PyTap sensor platform."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PORT,
    DOMAIN,
    STALE_TIMEOUT_SECONDS,
)
from custom_components.pytap.coordinator import (
    PyTapDataUpdateCoordinator,
    node_is_fresh,
)
from custom_components.pytap.sensor import (
    SENSOR_DESCRIPTIONS,
    _coerce_restored_state_value,
    async_setup_entry,
)

# Node timestamps must be recent, otherwise live readings are treated as stale
# (optimizers stop reporting at dusk) and sensors go unavailable.
FRESH_TS = dt_util.utcnow().isoformat()

MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
        CONF_MODULE_PEAK_POWER: 455,
    },
    {
        CONF_MODULE_STRING: "B",
        CONF_MODULE_NAME: "Panel_02",
        CONF_MODULE_BARCODE: "C-2345678D",
        CONF_MODULE_PEAK_POWER: 500,
    },
]

MOCK_NODE_DATA = {
    "A-1234567B": {
        "gateway_id": 1,
        "node_id": 10,
        "barcode": "A-1234567B",
        "name": "Panel_01",
        "string": "A",
        "voltage_in": 35.2,
        "voltage_out": 34.8,
        "current_in": 8.5,
        "current_out": 8.5977,
        "power": 299.2,
        "peak_power": 455,
        "performance": 65.76,
        "temperature": 42.0,
        "dc_dc_duty_cycle": 0.95,
        "rssi": -65,
        "daily_energy_wh": 123.45,
        "total_energy_wh": 4567.89,
        "readings_today": 42,
        "daily_reset_date": "2025-01-01",
        "last_update": FRESH_TS,
    },
}

MOCK_NODE_DATA_TWO = {
    "A-1234567B": {
        **MOCK_NODE_DATA["A-1234567B"],
        "power": 300.0,
        "peak_power": 455,
        "performance": 65.93,
        "daily_energy_wh": 120.0,
        "total_energy_wh": 4500.0,
        "readings_today": 15,
    },
    "C-2345678D": {
        "gateway_id": 1,
        "node_id": 11,
        "barcode": "C-2345678D",
        "name": "Panel_02",
        "string": "B",
        "voltage_in": 34.9,
        "voltage_out": 34.6,
        "current_in": 7.8,
        "current_out": 7.6,
        "power": 250.0,
        "peak_power": 500,
        "performance": 50.0,
        "temperature": 40.0,
        "dc_dc_duty_cycle": 0.9,
        "rssi": -66,
        "daily_energy_wh": 100.0,
        "total_energy_wh": 4100.0,
        "readings_today": 10,
        "daily_reset_date": "2025-01-01",
        "last_update": FRESH_TS,
    },
}


def _make_mock_config_entry(hass, modules=None):
    """Create a mock config entry for testing."""
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES if modules is None else modules,
    }
    entry.entry_id = "test_entry_id"
    entry.options = {}
    return entry


def _make_mock_coordinator(hass, entry, node_data=None):
    """Create a mock coordinator with test data."""
    coordinator = MagicMock(spec=PyTapDataUpdateCoordinator)
    coordinator.data = {
        "gateways": {},
        "nodes": node_data or {},
        "counters": {},
        "discovered_barcodes": [],
    }
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


async def test_sensor_entities_created(hass: HomeAssistant) -> None:
    """Test that sensor entities are created for each module/sensor combination."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []

    def capture_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, capture_entities)

    # 2 modules × 12 + 2 strings × 4 + installation 4 = 36 entities
    assert len(entities) == 36


async def test_sensor_unique_ids(hass: HomeAssistant) -> None:
    """Test that sensor unique IDs follow the expected pattern."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    unique_ids = {e.unique_id for e in entities}
    # Check a few expected unique IDs
    assert f"{DOMAIN}_A-1234567B_power" in unique_ids
    assert f"{DOMAIN}_A-1234567B_performance" in unique_ids
    assert f"{DOMAIN}_C-2345678D_rssi" in unique_ids
    assert f"{DOMAIN}_A-1234567B_temperature" in unique_ids
    assert f"{DOMAIN}_{entry.entry_id}_string_A_power" in unique_ids
    assert f"{DOMAIN}_{entry.entry_id}_installation_total_energy" in unique_ids


async def test_sensor_available_with_data(hass: HomeAssistant) -> None:
    """Test that sensors report available when node data is present."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    # Find the power sensor for S-1234567A (has data)
    power_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_power"
    )
    assert power_sensor.available is True

    # Find a sensor for C-2345678D (no data yet)
    power_sensor_b = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_C-2345678D_power"
    )
    assert power_sensor_b.available is False


async def test_sensor_unavailable_without_data(hass: HomeAssistant) -> None:
    """Test that sensors report unavailable when no node data exists."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data={})

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    for entity in entities:
        assert entity.available is False


async def test_sensor_skips_modules_without_barcode(hass: HomeAssistant) -> None:
    """Test that modules without a barcode are skipped."""
    entry = _make_mock_config_entry(hass)
    # Add a module without a barcode
    entry.data = {
        **entry.data,
        CONF_MODULES: [
            *MOCK_MODULES,
            {
                CONF_MODULE_STRING: "C",
                CONF_MODULE_NAME: "Unknown",
                CONF_MODULE_BARCODE: "",
            },
        ],
    }
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    # Only the 2 valid modules should contribute to entities: 2*12 + 2*4 + 4 = 36
    assert len(entities) == 36


async def test_sensor_device_info(hass: HomeAssistant) -> None:
    """Test that sensor entities have correct device info."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    power_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_power"
    )
    device_info = power_sensor.device_info
    assert (DOMAIN, "A-1234567B") in device_info["identifiers"]
    assert device_info["manufacturer"] == "Tigo Energy"
    assert device_info["model"] == "TS4"
    assert device_info["serial_number"] == "A-1234567B"


async def test_sensor_descriptions_count() -> None:
    """Test that we have the expected number of sensor descriptions."""
    assert len(SENSOR_DESCRIPTIONS) == 12


async def test_energy_sensor_descriptions() -> None:
    """Test energy sensor description metadata."""
    description_map = {
        description.key: description for description in SENSOR_DESCRIPTIONS
    }

    daily_energy = description_map["daily_energy"]
    assert daily_energy.native_unit_of_measurement == UnitOfEnergy.WATT_HOUR
    assert daily_energy.state_class == SensorStateClass.TOTAL

    total_energy = description_map["total_energy"]
    assert total_energy.native_unit_of_measurement == UnitOfEnergy.WATT_HOUR
    assert total_energy.state_class == SensorStateClass.TOTAL_INCREASING


async def test_daily_energy_last_reset(hass: HomeAssistant) -> None:
    """Test that daily energy exposes a last_reset timestamp."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    daily_energy_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_daily_energy"
    )
    assert daily_energy_sensor.last_reset is not None
    assert daily_energy_sensor.last_reset.isoformat().startswith("2025-01-01T00:00:00")


async def test_aggregate_entity_count_single_string(hass: HomeAssistant) -> None:
    """Two modules on one string produce 32 entities."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
            CONF_MODULE_PEAK_POWER: 455,
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
            CONF_MODULE_PEAK_POWER: 500,
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    assert len(entities) == 32


async def test_readings_today_sensor_metadata(hass: HomeAssistant) -> None:
    """Readings today sensor should be diagnostic and unitless TOTAL."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_readings_today"
    )
    assert sensor.entity_description.state_class == SensorStateClass.TOTAL
    assert sensor.entity_description.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.entity_description.native_unit_of_measurement is None


async def test_readings_today_value_and_last_reset(hass: HomeAssistant) -> None:
    """Readings today should expose stored count and daily last_reset."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_readings_today"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()

    assert sensor.native_value == 42
    assert sensor.last_reset is not None
    assert sensor.last_reset.isoformat().startswith("2025-01-01T00:00:00")


async def test_string_power_sums_constituents(hass: HomeAssistant) -> None:
    """String aggregate power should sum all members of the string."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
            CONF_MODULE_PEAK_POWER: 455,
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
            CONF_MODULE_PEAK_POWER: 500,
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_power"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 550.0


async def test_installation_power_sums_all(hass: HomeAssistant) -> None:
    """Installation aggregate power should sum all configured modules."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 550.0


async def test_string_daily_energy_sums(hass: HomeAssistant) -> None:
    """String aggregate daily energy should sum all members of the string."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
            CONF_MODULE_PEAK_POWER: 455,
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
            CONF_MODULE_PEAK_POWER: 500,
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_daily_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 220.0


async def test_string_total_energy_sums(hass: HomeAssistant) -> None:
    """String aggregate total energy should sum all members of the string."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
            CONF_MODULE_PEAK_POWER: 455,
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
            CONF_MODULE_PEAK_POWER: 500,
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_total_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 8600.0


async def test_installation_daily_energy_sums_all(hass: HomeAssistant) -> None:
    """Installation daily energy should sum all configured modules."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_daily_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 220.0


async def test_installation_total_energy_sums_all(hass: HomeAssistant) -> None:
    """Installation total energy should sum all configured modules."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_total_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 8600.0


async def test_aggregate_available_partial_data(hass: HomeAssistant) -> None:
    """Aggregate should be available if at least one source node reports."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert installation.available is True


async def test_aggregate_excludes_none_values(hass: HomeAssistant) -> None:
    """Aggregate sum should ignore nodes where value_key is None."""
    entry = _make_mock_config_entry(hass)
    node_data = {
        **MOCK_NODE_DATA_TWO,
        "C-2345678D": {**MOCK_NODE_DATA_TWO["C-2345678D"], "power": None},
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    installation.async_write_ha_state = lambda: None
    installation._handle_coordinator_update()
    assert installation.native_value == 300.0


async def test_performance_sensor_value(hass: HomeAssistant) -> None:
    """Per-optimizer performance sensor should expose stored percentage."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    performance_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_performance"
    )
    performance_sensor.async_write_ha_state = lambda: None
    performance_sensor._handle_coordinator_update()
    assert performance_sensor.native_value == 65.76


async def test_string_performance_weighted(hass: HomeAssistant) -> None:
    """String aggregate performance uses capacity-weighted formula."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
            CONF_MODULE_PEAK_POWER: 400,
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
            CONF_MODULE_PEAK_POWER: 500,
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    node_data = {
        "A-1234567B": {
            **MOCK_NODE_DATA_TWO["A-1234567B"],
            "power": 300.0,
            "peak_power": 400,
        },
        "C-2345678D": {
            **MOCK_NODE_DATA_TWO["C-2345678D"],
            "power": 200.0,
            "peak_power": 500,
        },
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_performance"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 55.56


async def test_installation_performance_partial_data(hass: HomeAssistant) -> None:
    """Aggregate performance should include only reporting nodes."""
    entry = _make_mock_config_entry(hass)
    node_data = {
        "A-1234567B": {
            **MOCK_NODE_DATA_TWO["A-1234567B"],
            "power": 300.0,
            "peak_power": 455,
        },
        "C-2345678D": {
            **MOCK_NODE_DATA_TWO["C-2345678D"],
            "power": None,
            "peak_power": 500,
        },
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_performance"
    )
    installation.async_write_ha_state = lambda: None
    installation._handle_coordinator_update()
    assert installation.native_value == round((300.0 / 455.0) * 100.0, 2)


async def test_installation_performance_unavailable_without_data(
    hass: HomeAssistant,
) -> None:
    """Aggregate performance should be unavailable when no nodes report power."""
    entry = _make_mock_config_entry(hass)
    node_data = {
        "A-1234567B": {**MOCK_NODE_DATA_TWO["A-1234567B"], "power": None},
        "C-2345678D": {**MOCK_NODE_DATA_TWO["C-2345678D"], "power": None},
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_performance"
    )
    installation.async_write_ha_state = lambda: None
    installation._handle_coordinator_update()
    assert installation.native_value is None


async def test_performance_sensor_zero_power(hass: HomeAssistant) -> None:
    """Power=0W should produce performance=0.0% (not None)."""
    entry = _make_mock_config_entry(hass)
    node_data = {
        "A-1234567B": {
            **MOCK_NODE_DATA["A-1234567B"],
            "power": 0.0,
            "performance": 0.0,
        },
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_performance"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 0.0


async def test_performance_sensor_above_100(hass: HomeAssistant) -> None:
    """Power exceeding peak_power should produce >100% (no clamping)."""
    entry = _make_mock_config_entry(hass)
    node_data = {
        "A-1234567B": {
            **MOCK_NODE_DATA["A-1234567B"],
            "power": 500.0,
            "peak_power": 455,
            "performance": round((500.0 / 455) * 100.0, 2),
        },
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_performance"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == round((500.0 / 455) * 100.0, 2)
    assert sensor.native_value > 100.0


async def test_aggregate_extra_attributes(hass: HomeAssistant) -> None:
    """Aggregate extra attrs should expose optimizer and reporting counts."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert installation.extra_state_attributes == {
        "optimizer_count": 2,
        "reporting_count": 1,
    }


async def test_string_aggregate_device_info(hass: HomeAssistant) -> None:
    """String aggregate should be attached to a virtual string device."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_power"
    )
    device_info = sensor.device_info
    assert (DOMAIN, f"{entry.entry_id}_string_A") in device_info["identifiers"]
    assert device_info["name"] == "Tigo String A"
    assert device_info["manufacturer"] == "Tigo Energy"
    assert device_info["model"] == "String Aggregate"


async def test_installation_aggregate_device_info(hass: HomeAssistant) -> None:
    """Installation aggregate should be attached to installation virtual device."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    device_info = sensor.device_info
    assert (DOMAIN, f"{entry.entry_id}_installation") in device_info["identifiers"]
    assert device_info["name"] == "Tigo Installation"
    assert device_info["manufacturer"] == "Tigo Energy"
    assert device_info["model"] == "Installation Aggregate"


async def test_aggregate_daily_energy_last_reset(hass: HomeAssistant) -> None:
    """Aggregate daily energy should have a local-midnight last_reset."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_daily_energy"
    )
    assert sensor.last_reset is not None
    assert sensor.last_reset.hour == 0
    assert sensor.last_reset.minute == 0
    assert sensor.last_reset.second == 0


async def test_aggregate_total_energy_no_last_reset(hass: HomeAssistant) -> None:
    """Aggregate total energy should not expose last_reset."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_total_energy"
    )
    assert sensor.last_reset is None


async def test_no_string_aggregates_when_no_modules(hass: HomeAssistant) -> None:
    """No entities should be created when there are no configured modules."""
    entry = _make_mock_config_entry(hass, modules=[])
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    assert entities == []


def test_coerce_restored_state_value_float_sensor() -> None:
    """Restored state should coerce to float for standard numeric sensors."""
    assert _coerce_restored_state_value("299.2", "power") == 299.2


def test_coerce_restored_state_value_int_sensor() -> None:
    """Readings-today restore should coerce to int."""
    assert _coerce_restored_state_value("42", "readings_today") == 42


def test_coerce_restored_state_value_ignores_unavailable() -> None:
    """Unavailable/unknown restore values should be ignored."""
    assert _coerce_restored_state_value("unknown", "power") is None
    assert _coerce_restored_state_value("unavailable", "power") is None


async def test_restart_restores_snapshot_before_live_stream(
    hass: HomeAssistant,
) -> None:
    """After restart, energy restores from snapshot; stale live readings do not."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: DEFAULT_PORT,
            CONF_MODULES: [MOCK_MODULES[0]],
        },
        entry_id="restart_restore_entry",
        title="PyTap (192.168.1.100)",
        version=4,
    )
    entry.add_to_hass(hass)

    today = dt_util.now().date().isoformat()
    stored_state = {
        "barcode_to_node": {"A-1234567B": 10},
        "discovered_barcodes": [],
        "energy_data": {
            "A-1234567B": {
                "daily_energy_wh": 12.5,
                "daily_reset_date": today,
                "total_energy_wh": 2000.0,
                "readings_today": 55,
                "last_power_w": 299.2,
                "last_reading_ts": "2026-02-24T20:00:00",
            }
        },
        "node_snapshots": {
            "A-1234567B": {
                "gateway_id": 1,
                "node_id": 10,
                "voltage_in": 35.2,
                "voltage_out": 34.8,
                "current_in": 8.5,
                "current_out": 8.4,
                "power": 299.2,
                "performance": 65.76,
                "temperature": 42.0,
                "dc_dc_duty_cycle": 0.95,
                "rssi": -65,
                "last_update": "2026-02-24T20:00:00",
            }
        },
    }

    with (
        patch(
            "custom_components.pytap.coordinator._MigratingStore.async_load",
            new=AsyncMock(return_value=stored_state),
        ),
        patch(
            "custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    power_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_power"
    )
    assert power_entity_id is not None
    power_state = hass.states.get(power_entity_id)
    assert power_state is not None
    # Snapshot is from hours ago — the module is not reporting now, so the
    # last-known power must not be presented as current: it reads 0 W.
    assert float(power_state.state) == 0.0

    rssi_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_rssi"
    )
    assert hass.states.get(rssi_entity_id).state == STATE_UNAVAILABLE

    # Verify energy sensors restore from coordinator snapshot (not zeroed)
    daily_energy_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_daily_energy"
    )
    assert daily_energy_entity_id is not None
    daily_energy_state = hass.states.get(daily_energy_entity_id)
    assert daily_energy_state is not None
    assert daily_energy_state.state != STATE_UNAVAILABLE
    assert float(daily_energy_state.state) == 12.5

    total_energy_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_total_energy"
    )
    assert total_energy_entity_id is not None
    total_energy_state = hass.states.get(total_energy_entity_id)
    assert total_energy_state is not None
    assert total_energy_state.state != STATE_UNAVAILABLE
    assert float(total_energy_state.state) == 2000.0

    installation_power_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert installation_power_entity_id is not None
    installation_power_state = hass.states.get(installation_power_entity_id)
    assert installation_power_state is not None
    assert float(installation_power_state.state) == 0.0

    installation_energy_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_total_energy"
    )
    installation_energy_state = hass.states.get(installation_energy_entity_id)
    assert float(installation_energy_state.state) == 2000.0


async def test_restart_uses_restore_entity_fallback_without_snapshot(
    hass: HomeAssistant,
) -> None:
    """Without a snapshot, energy sensors restore from the HA state cache.

    Live readings (power etc.) are never restored — they stay unavailable
    until the optimizer reports again.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: DEFAULT_PORT,
            CONF_MODULES: [MOCK_MODULES[0]],
        },
        entry_id="restart_restore_fallback_entry",
        title="PyTap (192.168.1.100)",
        version=4,
    )
    entry.add_to_hass(hass)

    stored_state = {
        "barcode_to_node": {},
        "discovered_barcodes": [],
    }

    with (
        patch(
            "custom_components.pytap.coordinator._MigratingStore.async_load",
            new=AsyncMock(return_value=stored_state),
        ),
        patch(
            "custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen",
            return_value=None,
        ),
        patch(
            "custom_components.pytap.sensor.PyTapSensor.async_get_last_sensor_data",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.pytap.sensor.PyTapAggregateSensor.async_get_last_sensor_data",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.pytap.sensor.PyTapSensor.async_get_last_state",
            new=AsyncMock(return_value=State("sensor.fake_module", "123.4")),
        ),
        patch(
            "custom_components.pytap.sensor.PyTapAggregateSensor.async_get_last_state",
            new=AsyncMock(return_value=State("sensor.fake_aggregate", "456.7")),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    power_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_power"
    )
    assert power_entity_id is not None
    power_state = hass.states.get(power_entity_id)
    assert power_state is not None
    assert power_state.state == STATE_UNAVAILABLE

    total_energy_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_total_energy"
    )
    total_energy_state = hass.states.get(total_energy_entity_id)
    assert total_energy_state.state != STATE_UNAVAILABLE
    assert float(total_energy_state.state) == 123.4

    installation_power_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert installation_power_entity_id is not None
    installation_power_state = hass.states.get(installation_power_entity_id)
    assert installation_power_state is not None
    assert installation_power_state.state == STATE_UNAVAILABLE

    installation_energy_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_total_energy"
    )
    installation_energy_state = hass.states.get(installation_energy_entity_id)
    assert installation_energy_state.state != STATE_UNAVAILABLE
    assert float(installation_energy_state.state) == 456.7


# ---------------------------------------------------------------------------
# Staleness — optimizers stop reporting at dusk
# ---------------------------------------------------------------------------


def test_node_is_fresh() -> None:
    """node_is_fresh compares last_update against the timeout."""
    now = dt_util.utcnow()
    recent = (now - timedelta(seconds=60)).isoformat()
    old = (now - timedelta(seconds=STALE_TIMEOUT_SECONDS + 1)).isoformat()
    assert node_is_fresh({"last_update": recent}, now) is True
    assert node_is_fresh({"last_update": old}, now) is False
    assert node_is_fresh({"last_update": None}, now) is False
    assert node_is_fresh({"last_update": "garbage"}, now) is False
    assert node_is_fresh(None, now) is False
    # Naive timestamps are interpreted as local time
    naive_local = dt_util.as_local(now - timedelta(seconds=30)).replace(tzinfo=None)
    assert node_is_fresh({"last_update": naive_local.isoformat()}, now) is True


def _stale_copy(node: dict) -> dict:
    stale_ts = dt_util.utcnow() - timedelta(seconds=STALE_TIMEOUT_SECONDS + 60)
    return {**node, "last_update": stale_ts.isoformat()}


async def test_live_sensors_when_stale(hass: HomeAssistant) -> None:
    """Quiet node: electrical readings -> 0, others unavailable, energy kept."""
    entry = _make_mock_config_entry(hass)
    node = MOCK_NODE_DATA["A-1234567B"]
    coordinator = _make_mock_coordinator(
        hass, entry, node_data={"A-1234567B": _stale_copy(node)}
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))
    by_id = {e.unique_id: e for e in entities}

    for key in (
        "power",
        "performance",
        "voltage_in",
        "voltage_out",
        "current_in",
        "current_out",
    ):
        sensor = by_id[f"{DOMAIN}_A-1234567B_{key}"]
        assert sensor.available is True, key
        sensor.async_write_ha_state = MagicMock()
        sensor._attr_native_value = 1.0
        sensor._handle_coordinator_update()
        assert sensor.native_value == 0.0, key

    for key in ("temperature", "dc_dc_duty_cycle", "rssi"):
        sensor = by_id[f"{DOMAIN}_A-1234567B_{key}"]
        assert sensor.available is False, key
        sensor.async_write_ha_state = MagicMock()
        sensor._attr_native_value = 1.0
        sensor._handle_coordinator_update()
        assert sensor.native_value is None, key

    for key in ("daily_energy", "total_energy", "readings_today"):
        sensor = by_id[f"{DOMAIN}_A-1234567B_{key}"]
        assert sensor.available is True, key


async def test_live_sensor_recovers_when_reports_resume(
    hass: HomeAssistant,
) -> None:
    """A stale sensor becomes available again on the next power report."""
    entry = _make_mock_config_entry(hass)
    node = MOCK_NODE_DATA["A-1234567B"]
    coordinator = _make_mock_coordinator(
        hass, entry, node_data={"A-1234567B": _stale_copy(node)}
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))
    power = next(e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_power")
    power.async_write_ha_state = MagicMock()
    power._handle_coordinator_update()
    assert power.native_value == 0.0

    coordinator.data["nodes"]["A-1234567B"] = {
        **node,
        "power": 12.0,
        "last_update": dt_util.utcnow().isoformat(),
    }
    power._handle_coordinator_update()
    assert power.available is True
    assert power.native_value == 12.0


async def test_aggregate_power_ignores_stale_nodes(hass: HomeAssistant) -> None:
    """Aggregate power sums only fresh nodes; energy sums all nodes."""
    entry = _make_mock_config_entry(hass)
    nodes = {
        "A-1234567B": MOCK_NODE_DATA_TWO["A-1234567B"],
        "C-2345678D": _stale_copy(MOCK_NODE_DATA_TWO["C-2345678D"]),
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=nodes)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))
    by_id = {e.unique_id: e for e in entities}
    for e in entities:
        e.async_write_ha_state = MagicMock()

    inst_power = by_id[f"{DOMAIN}_{entry.entry_id}_installation_power"]
    inst_power._handle_coordinator_update()
    assert inst_power.native_value == 300.0
    assert inst_power.extra_state_attributes == {
        "optimizer_count": 2,
        "reporting_count": 1,
    }

    inst_energy = by_id[f"{DOMAIN}_{entry.entry_id}_installation_total_energy"]
    inst_energy._handle_coordinator_update()
    assert inst_energy.native_value == 8600.0

    # String B has only the stale node -> 0 W / 0 %
    string_b_power = by_id[f"{DOMAIN}_{entry.entry_id}_string_B_power"]
    assert string_b_power.available is True
    string_b_power._handle_coordinator_update()
    assert string_b_power.native_value == 0.0
    string_b_perf = by_id[f"{DOMAIN}_{entry.entry_id}_string_B_performance"]
    string_b_perf._handle_coordinator_update()
    assert string_b_perf.native_value == 0.0

    # Strings/installation with no node ever seen stay unavailable
    coordinator.data["nodes"] = {}
    assert inst_power.available is False


async def test_coordinator_stale_check_notifies_listeners(
    hass: HomeAssistant,
) -> None:
    """The periodic check pushes an update when a node goes stale."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: DEFAULT_PORT,
            CONF_MODULES: [MOCK_MODULES[0]],
        },
        entry_id="stale_check_entry",
        version=4,
    )
    entry.add_to_hass(hass)
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator.data["nodes"]["A-1234567B"] = dict(MOCK_NODE_DATA["A-1234567B"])
    coordinator._fresh_barcodes = coordinator._compute_fresh_barcodes()
    assert coordinator._fresh_barcodes == {"A-1234567B"}

    with patch.object(coordinator, "async_update_listeners") as update:
        coordinator._async_check_stale()
        update.assert_not_called()

        coordinator.data["nodes"]["A-1234567B"] = _stale_copy(
            MOCK_NODE_DATA["A-1234567B"]
        )
        coordinator._async_check_stale()
        update.assert_called_once()
        assert coordinator._fresh_barcodes == frozenset()
