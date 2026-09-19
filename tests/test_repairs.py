"""Testy pro repairs.py.

Klíčová věc k ohlídání: `PendingDataRepairFlow.async_step_confirm` musí po
neúspěšném retry (den u distributora stále není uzavřený) vrátit
`async_abort`, ne `async_create_entry` — repairs manager v HA při
`async_create_entry` automaticky maže issue podle `issue_id`, takže by smazal
i nově vytvořený/stále platný issue (viz docstring v `repairs.py`).
"""

from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.predistribuce.const import DOMAIN
from custom_components.predistribuce.repairs import (
    PendingDataRepairFlow,
    async_create_fix_flow,
)


class FakeCoordinator:
    """Minimální náhrada za `PreDistribuceCoordinator` pro testy repair flow."""

    def __init__(self, *, still_pending: bool) -> None:
        self._still_pending = still_pending
        self.retry_calls = 0
        self.has_pending = True

    async def async_retry_pending(self) -> None:
        self.retry_calls += 1
        self.has_pending = self._still_pending


async def test_confirm_shows_form_first():
    flow = PendingDataRepairFlow(FakeCoordinator(still_pending=False))
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"


async def test_confirm_success_creates_entry():
    coordinator = FakeCoordinator(still_pending=False)
    flow = PendingDataRepairFlow(coordinator)

    result = await flow.async_step_confirm({})

    assert coordinator.retry_calls == 1
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_confirm_still_pending_aborts_without_deleting_issue():
    coordinator = FakeCoordinator(still_pending=True)
    flow = PendingDataRepairFlow(coordinator)

    result = await flow.async_step_confirm({})

    assert coordinator.retry_calls == 1
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "still_pending"


async def test_async_create_fix_flow_uses_entry_runtime_data(hass):
    coordinator = FakeCoordinator(still_pending=False)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator

    flow = await async_create_fix_flow(hass, "pending_data_x", {"entry_id": entry.entry_id})

    assert isinstance(flow, PendingDataRepairFlow)
    assert flow._coordinator is coordinator
