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

import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
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
                        CONF_IMPORT_HOUR: user_input[CONF_IMPORT_HOUR],
                        CONF_IMPORT_MINUTE: user_input[CONF_IMPORT_MINUTE],
                    },
                )

        schema = _eans_select_schema(
            self._available_points, [p.ean for p in self._available_points]
        ).extend(
            {
                vol.Required(
                    CONF_IMPORT_HOUR, default=DEFAULT_IMPORT_HOUR
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=23, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_IMPORT_MINUTE, default=DEFAULT_IMPORT_MINUTE
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=59, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
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
        return PreDistribuceOptionsFlow(config_entry)


class PreDistribuceOptionsFlow(config_entries.OptionsFlow):
    """Menu: změna času stahování, nebo přidání dalšího odběrného místa.

    Přihlašovací údaje jsou už uložené v `config_entry.data` — pro přidání
    odběrného místa se znovu použijí, uživatel je nezadává podruhé.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._available_points: list[pre_api.MeteringPoint] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["schedule", "metering_points"]
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data=_merged_options(self.config_entry, **user_input)
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
                {
                    vol.Required(
                        CONF_IMPORT_HOUR, default=current_hour
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=23, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_IMPORT_MINUTE, default=current_minute
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=59, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                }
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
