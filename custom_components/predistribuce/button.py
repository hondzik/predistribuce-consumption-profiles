"""Tlačítko pro manuální reimport dat, které při posledním běhu ještě nebyly
u distributora uzavřené (viz `PreDistribuceCoordinator._pending`)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PreDistribuceCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([PreDistribuceRetryButton(entry.runtime_data)])


class PreDistribuceRetryButton(ButtonEntity):
    """Zkusí znovu stáhnout dny, které se minule nepodařilo importovat."""

    _attr_has_entity_name = True
    _attr_translation_key = "retry_pending"

    def __init__(self, coordinator: PreDistribuceCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_retry_pending"

    async def async_press(self) -> None:
        await self._coordinator.async_retry_pending()
