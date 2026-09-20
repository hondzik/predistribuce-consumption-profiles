"""Testy pro config_flow.py (ConfigFlow + OptionsFlow) přes reálný HA loader.

`test_options_init_shows_menu` je regresní test na bug opravený
2026-09-19: `PreDistribuceOptionsFlow.__init__` si dřív ručně nastavoval
`self.config_entry`, což novější HA (config_entry dodává base třída
automaticky) shazovalo s 500 Internal Server Error při otevření
konfigurace existující entry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.predistribuce import pre_api
from custom_components.predistribuce.const import (
    CONF_EANS,
    CONF_IMPORT_HOUR,
    CONF_IMPORT_MINUTE,
    DOMAIN,
)

USERNAME = "user@example.cz"
PASSWORD = "heslo123"
EAN = "859182400306885078"
POINTS = [pre_api.MeteringPoint(ean=EAN, address="Testovací 123, Praha")]


def _entry_data() -> dict:
    return {
        "username": USERNAME,
        "password": PASSWORD,
        CONF_EANS: [EAN],
        CONF_IMPORT_HOUR: 6,
        CONF_IMPORT_MINUTE: 0,
    }


# ---------------------------------------------------------------------------
# async_step_user
# ---------------------------------------------------------------------------


async def test_user_step_shows_form_initially(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_step_success_advances_to_eans(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=POINTS),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "eans"


async def test_user_step_invalid_auth(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch.object(pre_api, "login", side_effect=pre_api.LoginError("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": "spatne"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch.object(pre_api, "login", side_effect=requests.ConnectionError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": PASSWORD}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_no_eans_found(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=[]),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": PASSWORD}
        )
    assert result["errors"] == {"base": "no_eans_found"}


# ---------------------------------------------------------------------------
# async_step_eans / plný flow
# ---------------------------------------------------------------------------


async def test_full_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=POINTS),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": PASSWORD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EANS: [EAN], "import_time": "07:30:00"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"PREdistribuce ({USERNAME})"
    assert result["data"] == {
        "username": USERNAME,
        "password": PASSWORD,
        CONF_EANS: [EAN],
        CONF_IMPORT_HOUR: 7,
        CONF_IMPORT_MINUTE: 30,
    }


async def test_eans_step_requires_selection(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=POINTS),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": PASSWORD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EANS: [], "import_time": "06:00:00"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "eans"
    assert result["errors"] == {"base": "no_eans_selected"}


async def test_duplicate_account_aborts_already_configured(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data()).add_to_hass(
        hass
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=POINTS),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": PASSWORD}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


async def test_reauth_success_updates_password(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    with patch.object(pre_api, "login"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "nove-heslo"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "nove-heslo"
    assert entry.data["username"] == USERNAME


async def test_reauth_invalid_password(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    with patch.object(pre_api, "login", side_effect=pre_api.LoginError("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "spatne"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == PASSWORD  # nezměněno


# ---------------------------------------------------------------------------
# options flow
# ---------------------------------------------------------------------------


async def test_options_init_shows_menu(hass):
    """Regresní test: options flow se musí otevřít bez 500 chyby."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) == {"schedule", "metering_points"}


async def test_options_schedule_updates_entry_options(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "schedule"}
    )
    assert result["step_id"] == "schedule"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"import_time": "08:15:00"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_IMPORT_HOUR] == 8
    assert entry.options[CONF_IMPORT_MINUTE] == 15


async def test_options_schedule_form_with_legacy_float_hour_minute(hass):
    """Regresní test: staré entry (uložené přes dřívější NumberSelector)
    mají hour/minute jako float — nesmí to shodit formulář ValueError."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={**_entry_data(), CONF_IMPORT_HOUR: 12.0, CONF_IMPORT_MINUTE: 10.0},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "schedule"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "schedule"


async def test_options_metering_points_success(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=POINTS),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "metering_points"}
        )
        assert result["step_id"] == "metering_points"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_EANS: [EAN]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EANS] == [EAN]


async def test_options_metering_points_invalid_auth_aborts(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(pre_api, "login", side_effect=pre_api.LoginError("nope")):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "metering_points"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


async def test_options_metering_points_no_selection_error(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data=_entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with (
        patch.object(pre_api, "login"),
        patch.object(pre_api, "list_metering_points", return_value=POINTS),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "metering_points"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_EANS: []}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_eans_selected"}
