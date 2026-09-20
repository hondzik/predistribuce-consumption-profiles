"""Service `predistribuce.import_historical_data` — manuální/historický import.

Primární API pro manuální (re)import konkrétního EANu a rozsahu dat.
Options-flow krok `historical_import` (`config_flow.py`) je jen tenká UI
vrstva nad tímto service (volá ho přes `hass.services.async_call`), aby
existovala jediná validační/importní logika — ne duplicitní volání
`coordinator.async_import_range` na dvou místech.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import CONF_EANS, DOMAIN

SERVICE_IMPORT_HISTORICAL_DATA = "import_historical_data"
ATTR_EAN = "ean"
ATTR_DATE_FROM = "date_from"
ATTR_DATE_TO = "date_to"

SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_EAN): cv.string,
        vol.Required(ATTR_DATE_FROM): cv.date,
        vol.Required(ATTR_DATE_TO): cv.date,
    }
)


def _get_loaded_entry(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_found"
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded"
        )
    return entry


def async_register_services(hass: HomeAssistant) -> None:
    """Zaregistruje service, pokud ještě není (voláno z každého `async_setup_entry`)."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA):
        return

    async def _async_handle_import_historical_data(call: ServiceCall) -> ServiceResponse:
        entry = _get_loaded_entry(hass, call.data[ATTR_CONFIG_ENTRY_ID])
        ean = call.data[ATTR_EAN]
        configured_eans = entry.options.get(CONF_EANS, entry.data.get(CONF_EANS, []))
        if ean not in configured_eans:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ean_not_configured",
                translation_placeholders={"ean": ean},
            )

        imported = await entry.runtime_data.async_import_range(
            ean, call.data[ATTR_DATE_FROM], call.data[ATTR_DATE_TO]
        )
        return {"imported": imported}

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORICAL_DATA,
        _async_handle_import_historical_data,
        schema=SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
