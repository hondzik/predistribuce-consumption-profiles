"""Setup integrace PREdistribuce."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import PreDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PreDistribuceCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # Dotáhne případný běh zameškaný výpadkem HA — nečeká se do dalšího
    # naplánovaného času. Vyhodí ConfigEntryNotReady/ConfigEntryAuthFailed
    # při chybě (obojí zpracuje `_fetch_and_parse` v coordinator.py).
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
