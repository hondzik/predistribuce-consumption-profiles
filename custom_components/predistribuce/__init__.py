"""Setup integrace PREdistribuce."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import PreDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PreDistribuceCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # Dotáhne případný běh zameškaný výpadkem HA — nečeká se do dalšího
    # naplánovaného času. Vyhodí ConfigEntryNotReady/ConfigEntryAuthFailed
    # při chybě (obojí zpracuje `_fetch_and_parse` v coordinator.py).
    # Chybějící/neuzavřená data se řeší přes repair issue (viz repairs.py),
    # integrace nemá žádnou entity platformu.
    await coordinator.async_config_entry_first_refresh()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
