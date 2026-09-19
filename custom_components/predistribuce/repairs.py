"""Repair flow pro issue `pending_data` (dny čekající na uzavření u distributora).

Nahrazuje dřívější `button.py` (`PreDistribuceRetryButton`) — akce „zkusit
znovu stáhnout" teď žije jen jako fix flow tohoto repair issue (Nastavení ->
Systém -> Opravy), integrace nemá žádnou trvalou entitu pro to.

Issue se vytváří/maže v `coordinator._notify_pending`. Pokud po pokusu o
reimport pořád něco chybí (den u distributora ještě není uzavřený), issue
se v `_notify_pending` znovu vytvoří se stejným `issue_id` — proto flow při
neúspěchu vrací `async_abort`, ne `async_create_entry` (ten by HA nechalo
issue smazat i když problém pořád trvá, protože dokončení fix flow s
`create_entry` v repairs manageru automaticky maže issue podle `issue_id`).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .coordinator import PreDistribuceCoordinator


class PendingDataRepairFlow(RepairsFlow):
    """Potvrzovací dialog: znovu zkusí stáhnout dny čekající na uzavření."""

    def __init__(self, coordinator: PreDistribuceCoordinator) -> None:
        self._coordinator = coordinator

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            await self._coordinator.async_retry_pending()
            if self._coordinator.has_pending:
                return self.async_abort(reason="still_pending")
            return self.async_create_entry(data={})

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    entry_id = (data or {})["entry_id"]
    entry = hass.config_entries.async_get_entry(entry_id)
    return PendingDataRepairFlow(entry.runtime_data)
