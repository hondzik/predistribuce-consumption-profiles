"""Coordinator pro PREdistribuce: denní import spotřeby jako externí LTS.

Vyžaduje běžící Home Assistant (importuje `homeassistant.*`) — na rozdíl od
`pre_api.py` není testovatelný samostatně přes `python3 coordinator.py`.

Denní běh stahuje jen "včerejšek" (nikdy dnešek — spotřeba za dnešek je
vždy nulová, viz CLAUDE.md). `async_import_range` je zároveň veřejná metoda
pro manuální/historický (re)import libovolného rozsahu (volá ji i repair
flow v `repairs.py`) — oba případy jdou přes stejnou logiku, aby počítání
běžícího `sum` bylo vždy konzistentní.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import requests

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import pre_api
from .const import CONF_EANS, CONF_IMPORT_HOUR, CONF_IMPORT_MINUTE, DOMAIN

_LOGGER = logging.getLogger(__name__)

TZ_PRAGUE = ZoneInfo("Europe/Prague")


class PreDistribuceCoordinator(DataUpdateCoordinator[None]):
    """Stahuje profily spotřeby a importuje je jako externí dlouhodobé statistiky.

    Neběží na `update_interval` (relativní interval by časem ujížděl) —
    spouští se přesně na uživatelem zadaný čas přes `async_track_time_change`.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        self._pending: dict[str, dt.date] = {}
        unsub = async_track_time_change(
            hass,
            self._handle_scheduled_run,
            hour=entry.options.get(CONF_IMPORT_HOUR, entry.data.get(CONF_IMPORT_HOUR)),
            minute=entry.options.get(CONF_IMPORT_MINUTE, entry.data.get(CONF_IMPORT_MINUTE)),
            second=0,
        )
        entry.async_on_unload(unsub)

    async def _handle_scheduled_run(self, _now: dt.datetime) -> None:
        await self.async_request_refresh()

    async def _async_update_data(self) -> None:
        """Denní běh: naimportuje včerejší den pro všechny nakonfigurované EANy."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        eans = self.entry.options.get(CONF_EANS, self.entry.data.get(CONF_EANS, []))
        for ean in eans:
            await self.async_import_range(ean, yesterday, yesterday)

    async def async_import_range(
        self, ean: str, date_from: dt.date, date_to: dt.date
    ) -> int:
        """Stáhne a naimportuje spotřebu za [date_from, date_to] pro daný EAN.

        `date_to` se vždy zarovná na nejdřív "včerejšek" — dnešek se nikdy
        nestahuje, spotřeba za něj je stejně vždy 0 (viz CLAUDE.md).
        Vrací počet naimportovaných hodinových záznamů (0 = nic nového,
        buď prázdný rozsah, nebo den(y) na konci ještě nejsou uzavřené u
        distributora — příští běh / manuální reimport to dožene).
        """
        date_to = min(date_to, dt.date.today() - dt.timedelta(days=1))
        if date_from > date_to:
            return 0

        readings = await self.hass.async_add_executor_job(
            self._fetch_and_parse, ean, date_from, date_to
        )
        hourly, first_unclosed_day = _aggregate_hourly(readings)

        if first_unclosed_day is not None:
            self._pending[ean] = first_unclosed_day
        else:
            self._pending.pop(ean, None)
        self._notify_pending()

        if not hourly:
            _LOGGER.debug(
                "%s: žádná uzavřená data v rozsahu %s–%s (den ještě nemusí být uzavřený)",
                ean, date_from, date_to,
            )
            return 0

        statistic_id = f"{DOMAIN}:{ean}_consumption"
        running_sum = await self._async_get_baseline_sum(statistic_id)
        stats: list[StatisticData] = []
        for hour_start, consumption_kwh in hourly:
            running_sum += consumption_kwh
            stats.append(
                StatisticData(start=hour_start, state=consumption_kwh, sum=running_sum)
            )

        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"PREdistribuce spotřeba {ean}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement="kWh",
        )
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.info("%s: naimportováno %d hodinových záznamů", ean, len(stats))
        return len(stats)

    @property
    def has_pending(self) -> bool:
        """True, pokud aspoň jeden EAN čeká na uzavření dne u distributora."""
        return bool(self._pending)

    @property
    def _issue_id(self) -> str:
        return f"pending_data_{self.entry.entry_id}"

    async def async_retry_pending(self) -> int:
        """Zkusí znovu doimportovat dny čekající na uzavření (repair issue).

        Volá se pro každý EAN, který má v `_pending` uložený den — dotáhne
        rozsah od toho dne po nejnovější dostupný ("včerejšek"). Pokud den
        pořád není uzavřený, `_pending`/issue zůstane beze změny.
        """
        total = 0
        for ean, pending_since in list(self._pending.items()):
            yesterday = dt.date.today() - dt.timedelta(days=1)
            total += await self.async_import_range(ean, pending_since, yesterday)
        return total

    def _notify_pending(self) -> None:
        """Vytvoří/aktualizuje nebo zruší repair issue o dnech čekajících na data."""
        if not self._pending:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return

        lines = "\n".join(
            f"- EAN {ean}: od {den.strftime('%d.%m.%Y')}"
            for ean, den in sorted(self._pending.items())
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pending_data",
            translation_placeholders={"lines": lines},
            data={"entry_id": self.entry.entry_id},
        )

    async def _async_get_baseline_sum(self, statistic_id: str) -> float:
        """Vrátí poslední známý `sum` pro danou statistiku, nebo 0 pro první import."""
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        rows = last.get(statistic_id)
        if not rows:
            return 0.0
        return rows[0]["sum"] or 0.0

    def _fetch_and_parse(
        self, ean: str, date_from: dt.date, date_to: dt.date
    ) -> list[pre_api.IntervalReading]:
        session = requests.Session()
        session.headers["User-Agent"] = pre_api.UA
        try:
            pre_api.login(
                session, self.entry.data["username"], self.entry.data["password"]
            )
        except pre_api.LoginError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        raw = pre_api.fetch_report_csv(
            session,
            ean,
            date_from.strftime("%d.%m.%Y"),
            date_to.strftime("%d.%m.%Y"),
        )
        return pre_api.parse_csv(raw)


def _aggregate_hourly(
    readings: list[pre_api.IntervalReading],
) -> tuple[list[tuple[dt.datetime, float]], dt.date | None]:
    """Agreguje 15min intervaly na hodinové kWh v UTC, jen za uzavřené dny.

    Den se považuje za neuzavřený (u distributora ještě nedorazil), pokud
    má úplně všechny intervaly spotřeby nulové — takový den i všechny za
    ním v rozsahu se přeskočí (chronologicky by neuzavřený den nemělo
    následovat uzavřené). Vrací dvojici (hodinová data, první neuzavřený
    den nebo None) — druhá hodnota je pro coordinator, aby věděl, na co
    má nastavit "čekající" reimport.

    Rozlišení duplicitní místní hodiny při přechodu na zimní čas (`fold`)
    je NEOVĚŘENÉ — žádný přechod nebyl v testovacích datech, nejbližší je
    25.10.2026 (viz CLAUDE.md, bod 6). Zkontrolovat hned po tomto datu.
    """
    by_day: dict[dt.date, list[pre_api.IntervalReading]] = {}
    for r in readings:
        by_day.setdefault(r.start.date(), []).append(r)

    result: list[tuple[dt.datetime, float]] = []
    first_unclosed_day: dt.date | None = None
    for day in sorted(by_day):
        day_readings = by_day[day]
        if not any((r.consumption_kwh or 0) > 0 for r in day_readings):
            _LOGGER.debug("den %s ještě není uzavřený (spotřeba celá 0)", day)
            first_unclosed_day = day
            break

        hourly: dict[dt.datetime, float] = {}
        seen_local_starts: set[dt.datetime] = set()
        for r in day_readings:
            fold = 1 if r.start in seen_local_starts else 0
            seen_local_starts.add(r.start)
            local_start = r.start.replace(tzinfo=TZ_PRAGUE, fold=fold)
            hour_start = local_start.replace(minute=0, second=0, microsecond=0)
            hourly[hour_start] = hourly.get(hour_start, 0.0) + (r.consumption_kwh or 0.0)

        for hour_start in sorted(hourly):
            result.append((hour_start.astimezone(dt.timezone.utc), round(hourly[hour_start], 3)))

    return result, first_unclosed_day
