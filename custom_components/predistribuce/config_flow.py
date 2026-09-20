"""Config flow pro PREdistribuce.

Krok 1 (`user`): přihlašovací údaje, ověřené živým loginem přes `pre_api`
(běží v executoru — blokující síťové I/O). Zároveň se z účtu zjistí
dostupná odběrná místa (`pre_api.list_metering_points`), aby EAN uživatel
nemusel přepisovat ručně z portálu.

Krok 2 (`eans`): checkboxy pro výběr, které z nalezených odběrných míst se
mají importovat (musí být vybráno alespoň jedno), a čas denního stažení.

Options flow (menu): "schedule" pro změnu času bez nutnosti znovu zadávat
heslo, a "metering_points" pro dodatečné přidání dalšího odběrného místa
později — přihlašovací údaje se znovu použijí z `entry.data`, uživatel je
nezadává podruhé.

Pozn.: až se bude psát tlačítko pro manuální import historických dat,
musí umožnit vybrat KONKRÉTNÍ odběrné místo (EAN), ne jen spustit import
pro všechny nakonfigurované najednou — viz CLAUDE.md.

Pozn.: importy z `homeassistant.*` nejsou testovatelné bez běžícího HA.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from . import pre_api
from .const import (
    CONF_EANS,
    CONF_IMPORT_HOUR,
    CONF_IMPORT_MINUTE,
    DEFAULT_IMPORT_HOUR,
    DEFAULT_IMPORT_MINUTE,
    DOMAIN,
)
from .services import ATTR_DATE_FROM, ATTR_DATE_TO, ATTR_EAN, SERVICE_IMPORT_HISTORICAL_DATA

_LOGGER = logging.getLogger(__name__)


def _login_and_list_points(username: str, password: str) -> list[pre_api.MeteringPoint]:
    """Přihlásí se a vrátí odběrná místa na účtu. Volá se přes executor job."""
    session = requests.Session()
    session.headers["User-Agent"] = pre_api.UA
    pre_api.login(session, username, password)
    return pre_api.list_metering_points(session)


def _login_only(username: str, password: str) -> None:
    """Jen ověří přihlášení (reauth) — nepotřebuje seznam odběrných míst."""
    session = requests.Session()
    session.headers["User-Agent"] = pre_api.UA
    pre_api.login(session, username, password)


def _points_to_options(points: list[pre_api.MeteringPoint]) -> list[dict[str, str]]:
    return [
        {"value": p.ean, "label": f"{p.ean} — {p.address}" if p.address else p.ean}
        for p in points
    ]


def _eans_select_schema(points: list[pre_api.MeteringPoint], default: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EANS, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_points_to_options(points),
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _merged_options(entry: config_entries.ConfigEntry, **updates: Any) -> dict[str, Any]:
    merged = dict(entry.options)
    merged.update(updates)
    return merged


CONF_IMPORT_TIME = "import_time"


def _import_time_schema_field(default_hour: int, default_minute: int) -> dict[Any, Any]:
    """Jedno pole s nativním time pickerem místo dvou číselníků hodina/minuta.

    Staré config entries mohou mít hour/minute uložené jako float — dřívější
    NumberSelector vždy vracel float, i pro celá čísla (ověřeno v HA core
    zdrojáku). `:02d` na float spadne s ValueError, proto explicitní int().
    """
    default_hour, default_minute = int(default_hour), int(default_minute)
    return {
        vol.Required(
            CONF_IMPORT_TIME, default=f"{default_hour:02d}:{default_minute:02d}:00"
        ): selector.TimeSelector(),
    }


def _hour_minute_from_import_time(user_input: dict[str, Any]) -> dict[str, int]:
    hour, minute, *_rest = user_input[CONF_IMPORT_TIME].split(":")
    return {CONF_IMPORT_HOUR: int(hour), CONF_IMPORT_MINUTE: int(minute)}


class PreDistribuceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Nastavení integrace: přihlášení -> výběr odběrných míst a času stahování."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._available_points: list[pre_api.MeteringPoint] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input["username"]
            self._password = user_input["password"]
            try:
                self._available_points = await self.hass.async_add_executor_job(
                    _login_and_list_points, self._username, self._password
                )
            except pre_api.LoginError:
                errors["base"] = "invalid_auth"
            except requests.RequestException:
                errors["base"] = "cannot_connect"
            else:
                if not self._available_points:
                    errors["base"] = "no_eans_found"
                else:
                    await self.async_set_unique_id(self._username)
                    self._abort_if_unique_id_configured()
                    return await self.async_step_eans()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_eans(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_EANS]:
                errors["base"] = "no_eans_selected"
            else:
                return self.async_create_entry(
                    title=f"PREdistribuce ({self._username})",
                    data={
                        "username": self._username,
                        "password": self._password,
                        CONF_EANS: user_input[CONF_EANS],
                        **_hour_minute_from_import_time(user_input),
                    },
                )

        schema = _eans_select_schema(
            self._available_points, [p.ean for p in self._available_points]
        ).extend(_import_time_schema_field(DEFAULT_IMPORT_HOUR, DEFAULT_IMPORT_MINUTE))
        return self.async_show_form(step_id="eans", data_schema=schema, errors=errors)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Spustí se, když `coordinator.py` vyhodí `ConfigEntryAuthFailed`."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    _login_only, reauth_entry.data["username"], user_input["password"]
                )
            except pre_api.LoginError:
                errors["base"] = "invalid_auth"
            except requests.RequestException:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, "password": user_input["password"]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"username": reauth_entry.data["username"]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "PreDistribuceOptionsFlow":
        return PreDistribuceOptionsFlow()


class PreDistribuceOptionsFlow(config_entries.OptionsFlow):
    """Menu: změna času stahování, nebo přidání dalšího odběrného místa.

    Přihlašovací údaje jsou už uložené v `config_entry.data` — pro přidání
    odběrného místa se znovu použijí, uživatel je nezadává podruhé.

    Pozn.: `self.config_entry` se NEnastavuje v `__init__` — novější HA
    (breaking change, config_entry teď dodává base třída/flow manager
    automaticky) by na explicitní nastavení shodilo options flow s
    500 Internal Server Error při otevření.
    """

    def __init__(self) -> None:
        self._available_points: list[pre_api.MeteringPoint] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["schedule", "metering_points", "historical_import"],
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data=_merged_options(
                    self.config_entry, **_hour_minute_from_import_time(user_input)
                )
            )

        current_hour = self.config_entry.options.get(
            CONF_IMPORT_HOUR,
            self.config_entry.data.get(CONF_IMPORT_HOUR, DEFAULT_IMPORT_HOUR),
        )
        current_minute = self.config_entry.options.get(
            CONF_IMPORT_MINUTE,
            self.config_entry.data.get(CONF_IMPORT_MINUTE, DEFAULT_IMPORT_MINUTE),
        )
        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                _import_time_schema_field(current_hour, current_minute)
            ),
        )

    async def async_step_metering_points(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._available_points is None:
            try:
                self._available_points = await self.hass.async_add_executor_job(
                    _login_and_list_points,
                    self.config_entry.data["username"],
                    self.config_entry.data["password"],
                )
            except pre_api.LoginError:
                return self.async_abort(reason="invalid_auth")
            except requests.RequestException:
                return self.async_abort(reason="cannot_connect")

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_EANS]:
                errors["base"] = "no_eans_selected"
            else:
                return self.async_create_entry(
                    data=_merged_options(self.config_entry, **user_input)
                )

        current_eans = self.config_entry.options.get(
            CONF_EANS, self.config_entry.data.get(CONF_EANS, [])
        )
        schema = _eans_select_schema(self._available_points, current_eans)
        return self.async_show_form(
            step_id="metering_points", data_schema=schema, errors=errors
        )

    async def async_step_historical_import(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Tenká UI vrstva nad service `predistribuce.import_historical_data`.

        Validace (EAN patří k účtu, přihlášení, samotný import) žije jen
        v `services.py` — tenhle krok jen vyplní formulář a zavolá service,
        aby existovala jediná logika pro obě cesty (Developer Tools i UI).
        """
        configured_eans = self.config_entry.options.get(
            CONF_EANS, self.config_entry.data.get(CONF_EANS, [])
        )
        if not configured_eans:
            return self.async_abort(reason="no_eans_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                response = await self.hass.services.async_call(
                    DOMAIN,
                    SERVICE_IMPORT_HISTORICAL_DATA,
                    {
                        "config_entry_id": self.config_entry.entry_id,
                        ATTR_EAN: user_input[ATTR_EAN],
                        ATTR_DATE_FROM: user_input[ATTR_DATE_FROM],
                        ATTR_DATE_TO: user_input[ATTR_DATE_TO],
                    },
                    blocking=True,
                    return_response=True,
                )
            except HomeAssistantError:
                errors["base"] = "import_failed"
            else:
                imported = response["imported"] if response else 0
                return self.async_abort(
                    reason="historical_import_done",
                    description_placeholders={"count": str(imported)},
                )

        schema = vol.Schema(
            {
                vol.Required(ATTR_EAN, default=configured_eans[0]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=configured_eans,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(ATTR_DATE_FROM): selector.DateSelector(),
                vol.Required(
                    ATTR_DATE_TO,
                    default=(dt.date.today() - dt.timedelta(days=1)).isoformat(),
                ): selector.DateSelector(),
            }
        )
        return self.async_show_form(
            step_id="historical_import", data_schema=schema, errors=errors
        )
