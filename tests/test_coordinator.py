"""Testy pro coordinator.py: agregace 15min intervalů na hodiny, running `sum`
pro externí statistiky a repair-issue signalizaci neuzavřených dnů.

`get_last_statistics`/`async_add_external_statistics` jsou zde mockované na
úrovni modulu — reálný zápis/čtení z recorderu by testy zbytečně zpomalil a
zkomplikoval (timing kolem flush workeru); logika running `sum` a agregace
se testuje samostatně a je to to, co v `coordinator.py` není triviální.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.predistribuce import pre_api
from custom_components.predistribuce.const import (
    CONF_EANS,
    CONF_IMPORT_HOUR,
    CONF_IMPORT_MINUTE,
    DOMAIN,
)
from custom_components.predistribuce.coordinator import (
    PreDistribuceCoordinator,
    _aggregate_hourly,
)
from homeassistant.helpers import issue_registry as ir

USERNAME = "user@example.cz"
PASSWORD = "heslo123"
EAN = "859182400306885078"
DAY = dt.date(2026, 9, 17)
STATISTIC_ID = f"{DOMAIN}:{EAN}_consumption"


def _entry_data() -> dict:
    return {
        "username": USERNAME,
        "password": PASSWORD,
        CONF_EANS: [EAN],
        CONF_IMPORT_HOUR: 6,
        CONF_IMPORT_MINUTE: 0,
    }


def _reading(
    hour: int, minute: int, consumption: float | None, day: dt.date = DAY
) -> pre_api.IntervalReading:
    start = dt.datetime.combine(day, dt.time(hour, minute))
    return pre_api.IntervalReading(
        start=start,
        end=start + dt.timedelta(minutes=15),
        consumption_kwh=consumption,
        power_kw=1.0,
    )


def _make_coordinator(hass) -> tuple[PreDistribuceCoordinator, MockConfigEntry]:
    """Vytvoří coordinator bez registrace reálného `async_track_time_change`.

    Testy tady necílí na denní plánování (`_handle_scheduled_run`) — bez
    tohoto patchu by po testu zůstal viset neuklizený timer (`entry.async_on_
    unload` se spustí až při unloadu entry, který se v těchto testech vůbec
    nevolá) a `pytest-homeassistant-custom-component` by test shodil na
    "Lingering timer after test".
    """
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data())
    entry.add_to_hass(hass)
    with patch(
        "custom_components.predistribuce.coordinator.async_track_time_change",
        return_value=lambda: None,
    ):
        coordinator = PreDistribuceCoordinator(hass, entry)
    return coordinator, entry


# ---------------------------------------------------------------------------
# _aggregate_hourly (čistá funkce)
# ---------------------------------------------------------------------------


def test_aggregate_hourly_sums_quarter_hours_into_hour():
    readings = [
        _reading(10, 0, 0.1),
        _reading(10, 15, 0.2),
        _reading(10, 30, 0.3),
        _reading(10, 45, 0.1),
    ]

    hourly, first_unclosed_day = _aggregate_hourly(readings)

    assert first_unclosed_day is None
    assert len(hourly) == 1
    hour_start_utc, consumption = hourly[0]
    # 17.9. je v CEST (UTC+2) -> 10:00 lokálně je 08:00 UTC.
    assert hour_start_utc == dt.datetime(2026, 9, 17, 8, 0, tzinfo=dt.timezone.utc)
    assert consumption == 0.7


def test_aggregate_hourly_all_zero_day_is_unclosed():
    readings = [_reading(0, 0, 0.0), _reading(0, 15, 0.0)]

    hourly, first_unclosed_day = _aggregate_hourly(readings)

    assert hourly == []
    assert first_unclosed_day == DAY


def test_aggregate_hourly_stops_at_first_unclosed_day():
    closed_day = DAY
    unclosed_day = DAY + dt.timedelta(days=1)
    readings = [
        _reading(10, 0, 0.5, day=closed_day),
        _reading(10, 0, 0.0, day=unclosed_day),
    ]

    hourly, first_unclosed_day = _aggregate_hourly(readings)

    assert len(hourly) == 1
    assert first_unclosed_day == unclosed_day


def test_aggregate_hourly_treats_none_consumption_as_zero():
    readings = [_reading(10, 0, None), _reading(10, 15, None)]

    hourly, first_unclosed_day = _aggregate_hourly(readings)

    assert hourly == []
    assert first_unclosed_day == DAY


# ---------------------------------------------------------------------------
# async_import_range
# ---------------------------------------------------------------------------


async def test_import_range_first_import_baseline_zero(hass):
    coordinator, _entry = _make_coordinator(hass)
    readings = [_reading(10, 0, 0.4), _reading(10, 15, 0.6)]
    coordinator._fetch_and_parse = MagicMock(return_value=readings)

    with (
        patch(
            "custom_components.predistribuce.coordinator.get_last_statistics",
            return_value={},
        ),
        patch(
            "custom_components.predistribuce.coordinator.async_add_external_statistics"
        ) as mock_add_stats,
    ):
        count = await coordinator.async_import_range(EAN, DAY, DAY)

    assert count == 1
    stats = mock_add_stats.call_args.args[2]
    assert stats[0]["state"] == 1.0
    assert stats[0]["sum"] == 1.0
    assert coordinator.has_pending is False


async def test_import_range_continues_running_sum_from_baseline(hass):
    coordinator, _entry = _make_coordinator(hass)
    readings = [_reading(10, 0, 1.0)]
    coordinator._fetch_and_parse = MagicMock(return_value=readings)

    with (
        patch(
            "custom_components.predistribuce.coordinator.get_last_statistics",
            return_value={STATISTIC_ID: [{"sum": 10.0}]},
        ),
        patch(
            "custom_components.predistribuce.coordinator.async_add_external_statistics"
        ) as mock_add_stats,
    ):
        await coordinator.async_import_range(EAN, DAY, DAY)

    stats = mock_add_stats.call_args.args[2]
    assert stats[0]["sum"] == 11.0


async def test_import_range_clamps_date_to_yesterday_and_skips_future(hass):
    coordinator, _entry = _make_coordinator(hass)
    coordinator._fetch_and_parse = MagicMock()
    today = dt.date.today()

    count = await coordinator.async_import_range(EAN, today, today)

    assert count == 0
    coordinator._fetch_and_parse.assert_not_called()


async def test_import_range_unclosed_day_sets_pending_and_creates_issue(hass):
    coordinator, entry = _make_coordinator(hass)
    coordinator._fetch_and_parse = MagicMock(
        return_value=[_reading(0, 0, 0.0), _reading(0, 15, 0.0)]
    )

    with patch(
        "custom_components.predistribuce.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        count = await coordinator.async_import_range(EAN, DAY, DAY)

    assert count == 0
    mock_add_stats.assert_not_called()
    assert coordinator.has_pending is True

    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._issue_id)
    assert issue is not None
    assert EAN in issue.translation_placeholders["lines"]


async def test_import_range_resolves_pending_and_deletes_issue(hass):
    coordinator, entry = _make_coordinator(hass)
    coordinator._fetch_and_parse = MagicMock(
        return_value=[_reading(0, 0, 0.0), _reading(0, 15, 0.0)]
    )
    with patch(
        "custom_components.predistribuce.coordinator.async_add_external_statistics"
    ):
        await coordinator.async_import_range(EAN, DAY, DAY)
    assert coordinator.has_pending is True

    coordinator._fetch_and_parse = MagicMock(return_value=[_reading(10, 0, 0.5)])
    with (
        patch(
            "custom_components.predistribuce.coordinator.get_last_statistics",
            return_value={},
        ),
        patch(
            "custom_components.predistribuce.coordinator.async_add_external_statistics"
        ),
    ):
        await coordinator.async_import_range(EAN, DAY, DAY)

    assert coordinator.has_pending is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._issue_id)
    assert issue is None


async def test_async_retry_pending_reimports_from_pending_day(hass):
    coordinator, _entry = _make_coordinator(hass)
    pending_since = DAY
    coordinator._pending[EAN] = pending_since
    coordinator.async_import_range = AsyncMock(return_value=5)

    total = await coordinator.async_retry_pending()

    yesterday = dt.date.today() - dt.timedelta(days=1)
    coordinator.async_import_range.assert_awaited_once_with(EAN, pending_since, yesterday)
    assert total == 5
