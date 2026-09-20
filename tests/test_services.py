"""Testy pro service `predistribuce.import_historical_data` (`services.py`).

`coordinator.async_import_range` je mockovaná — kryje se jen validace/routing
v `services.py` (dohledání config entry, kontrola stavu, kontrola EANu),
samotný import má vlastní testy v `test_coordinator.py`.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.predistribuce.const import CONF_EANS, DOMAIN
from custom_components.predistribuce.coordinator import PreDistribuceCoordinator
from custom_components.predistribuce.services import (
    SERVICE_IMPORT_HISTORICAL_DATA,
    async_register_services,
)

USERNAME = "user@example.cz"
PASSWORD = "heslo123"
EAN = "859182400306885078"
OTHER_EAN = "111111111111111111"


def _entry_data() -> dict:
    return {
        "username": USERNAME,
        "password": PASSWORD,
        CONF_EANS: [EAN],
        "import_hour": 6,
        "import_minute": 0,
    }


def _make_loaded_entry(hass) -> tuple[MockConfigEntry, PreDistribuceCoordinator]:
    """Vytvoří LOADED config entry s coordinatorem a mockovaným `async_import_range`.

    `async_track_time_change` je patchnutý na no-op ze stejného důvodu jako v
    `test_coordinator.py`'s `_make_coordinator` — jinak by po testu zůstal
    viset neuklizený denní timer.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data())
    entry.add_to_hass(hass)
    with patch(
        "custom_components.predistribuce.coordinator.async_track_time_change",
        return_value=lambda: None,
    ):
        coordinator = PreDistribuceCoordinator(hass, entry)
    coordinator.async_import_range = AsyncMock(return_value=7)
    entry.runtime_data = coordinator
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry, coordinator


async def test_service_calls_coordinator_and_returns_count(hass):
    entry, coordinator = _make_loaded_entry(hass)
    async_register_services(hass)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_HISTORICAL_DATA,
        {
            "config_entry_id": entry.entry_id,
            "ean": EAN,
            "date_from": "2026-09-01",
            "date_to": "2026-09-02",
        },
        blocking=True,
        return_response=True,
    )

    coordinator.async_import_range.assert_awaited_once_with(
        EAN, dt.date(2026, 9, 1), dt.date(2026, 9, 2)
    )
    assert response == {"imported": 7}


async def test_service_rejects_unknown_entry(hass):
    async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            {
                "config_entry_id": "does-not-exist",
                "ean": EAN,
                "date_from": "2026-09-01",
                "date_to": "2026-09-02",
            },
            blocking=True,
        )


async def test_service_rejects_not_loaded_entry(hass):
    entry, coordinator = _make_loaded_entry(hass)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            {
                "config_entry_id": entry.entry_id,
                "ean": EAN,
                "date_from": "2026-09-01",
                "date_to": "2026-09-02",
            },
            blocking=True,
        )
    coordinator.async_import_range.assert_not_called()


async def test_service_rejects_unconfigured_ean(hass):
    entry, coordinator = _make_loaded_entry(hass)
    async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            {
                "config_entry_id": entry.entry_id,
                "ean": OTHER_EAN,
                "date_from": "2026-09-01",
                "date_to": "2026-09-02",
            },
            blocking=True,
        )
    coordinator.async_import_range.assert_not_called()


async def test_service_rejects_date_from_after_date_to(hass):
    entry, coordinator = _make_loaded_entry(hass)
    async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            {
                "config_entry_id": entry.entry_id,
                "ean": EAN,
                "date_from": "2026-09-05",
                "date_to": "2026-09-01",
            },
            blocking=True,
        )
    coordinator.async_import_range.assert_not_called()


def test_async_register_services_is_idempotent(hass):
    async_register_services(hass)
    async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA)
