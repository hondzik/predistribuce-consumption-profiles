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
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_conversion import EnergyConverter

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
        range_start_utc = dt.datetime.combine(
            date_from, dt.time.min, tzinfo=TZ_PRAGUE
        ).astimezone(dt.timezone.utc)
        running_sum = await self._async_get_baseline_sum(statistic_id, range_start_utc)

        stats: list[StatisticData] = []
        for hour_start, consumption_kwh in hourly:
            running_sum += consumption_kwh
            stats.append(
                StatisticData(start=hour_start, state=consumption_kwh, sum=running_sum)
            )
        new_count = len(stats)

        # Backfill dřívějšího rozsahu k už existujícím pozdějším dnům (např.
        # historický import po jednotlivých dnech v libovolném pořadí) — pokud
        # po `date_to` už nějaká data existují, jejich `sum` musí navázat na
        # `running_sum` spočítaný výše, jinak by na hranici vznikl skok dolů
        # (ověřeno živě 2026-09-20, viz CLAUDE.md). Běžný denní běh (`date_to`
        # == včerejšek) žádná pozdější data mít nemůže — dotaz se přeskočí.
        yesterday = dt.date.today() - dt.timedelta(days=1)
        if date_to < yesterday:
            tail_start_utc = hourly[-1][0] + dt.timedelta(hours=1)
            for row in await self._async_get_existing_rows_from(
                statistic_id, tail_start_utc
            ):
                running_sum += row["state"] or 0.0
                stats.append(
                    StatisticData(
                        start=dt.datetime.fromtimestamp(row["start"], tz=dt.timezone.utc),
                        state=row["state"],
                        sum=running_sum,
                    )
                )

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"PREdistribuce spotřeba {ean}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement="kWh",
        )
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.info("%s: naimportováno %d hodinových záznamů", ean, new_count)
        return new_count

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

    async def _async_get_baseline_sum(
        self, statistic_id: str, before: dt.datetime
    ) -> float:
        """Vrátí `sum` posledního existujícího záznamu s `start < before`, jinak 0.

        Záměrně hledá poslední bod PŘED začátkem importovaného rozsahu, ne
        poslední bod vůbec (`get_last_statistics`) — ten by při backfillu
        dřívějšího rozsahu k už existujícím pozdějším dnům vracel špatnou
        (pozdější) baseline a na hranici by vznikl skok v `sum` dolů
        (ověřeno živě 2026-09-20, viz CLAUDE.md).
        """
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc),
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        existing = rows.get(statistic_id)
        if not existing:
            return 0.0
        return existing[-1]["sum"] or 0.0

    async def _async_get_existing_rows_from(
        self, statistic_id: str, start: dt.datetime
    ) -> list[dict]:
        """Vrátí existující hodinové záznamy (`start`/`state`) od `start` dál."""
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            None,
            {statistic_id},
            "hour",
            None,
            {"state"},
        )
        return rows.get(statistic_id, [])

    def _fetch_and_parse(
        self, ean: str, date_from: dt.date, date_to: dt.date
    ) -> list[pre_api.IntervalReading]:
        session = requests.Session()
        session.headers["User-Agent"] = pre_api.UA
        try:
            pre_api.login(
                session, self.entry.data["username"], self.entry.data["password"]
            )
            raw = pre_api.fetch_report_csv(
                session,
                ean,
                date_from.strftime("%d.%m.%Y"),
                date_to.strftime("%d.%m.%Y"),
            )
        except pre_api.LoginError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except requests.exceptions.RequestException as err:
            # Bez tohohle by síťová chyba (výpadek portálu, timeout) proletěla
            # jako nezachycená requests výjimka až do frontendu (options-flow
            # krok/service) jako "unknown error" bez smysluplné zprávy
            # (ověřeno živě 2026-09-20, viz CLAUDE.md).
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        return pre_api.parse_csv(raw)


def _aggregate_hourly(
    readings: list[pre_api.IntervalReading],
) -> tuple[list[tuple[dt.datetime, float]], dt.date | None]:
    """Agreguje 15min intervaly na hodinové kWh v UTC, jen za uzavřené dny.

    Den se považuje za neuzavřený (u distributora ještě nedorazil), pokud
    má úplně všechny intervaly spotřeby nulové. Takto neuzavřené dny se
    ale hledají jen jako KONCOVÁ (trailing) série od nejnovějšího dne v
    rozsahu směrem zpět — ne jako první nulový den chronologicky odspodu
    (nalezená a opravená chyba 2026-09-20, viz CLAUDE.md: historický import
    rozsahu začínajícího dnem mimo dostupné období portálu, kde první den
    vyjde celý nulový, dřív zahodil i všechny reálné dny za ním). Jakýkoli
    nulový den, který NENÍ součástí této koncové série (tedy má za sebou i
    den s reálnými daty), se naimportuje normálně s nulovou spotřebou —
    není důvod ho považovat za "čekající na uzavření". Vrací dvojici
    (hodinová data, nejstarší den z koncové neuzavřené série nebo None) —
    druhá hodnota je pro coordinator, aby věděl, na co má nastavit
    "čekající" reimport.

    Rozlišení duplicitní místní hodiny při přechodu na zimní čas (`fold`)
    je NEOVĚŘENÉ — žádný přechod nebyl v testovacích datech, nejbližší je
    25.10.2026 (viz CLAUDE.md, bod 6). Zkontrolovat hned po tomto datu.
    """
    by_day: dict[dt.date, list[pre_api.IntervalReading]] = {}
    for r in readings:
        by_day.setdefault(r.start.date(), []).append(r)

    def _is_all_zero(day: dt.date) -> bool:
        return not any((r.consumption_kwh or 0) > 0 for r in by_day[day])

    sorted_days = sorted(by_day)
    trailing_unclosed: set[dt.date] = set()
    for day in reversed(sorted_days):
        if not _is_all_zero(day):
            break
        _LOGGER.debug("den %s ještě není uzavřený (spotřeba celá 0)", day)
        trailing_unclosed.add(day)

    first_unclosed_day = min(trailing_unclosed) if trailing_unclosed else None

    result: list[tuple[dt.datetime, float]] = []
    for day in sorted_days:
        if day in trailing_unclosed:
            continue
        day_readings = by_day[day]

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
