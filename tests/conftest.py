"""Fixtures pro testy vyžadující Home Assistant (`config_flow.py`, `coordinator.py`,
`repairs.py`).

`pre_api.py` testy (`tests/test_pre_api.py`) homeassistant nepotřebují a
importují ho jako čistý top-level modul (viz `pyproject.toml`). Tyto testy
naopak importují integraci jako běžný balíček
`custom_components.predistribuce` přes skutečný HA loader/`ConfigEntry`.

Poznámka: `homeassistant` core (a tedy `pytest-homeassistant-custom-
component`) importuje na modulové úrovni `fcntl`, který na Windows
neexistuje — tyhle testy proto na nativním Windows Pythonu vůbec NEJDOU
spustit/sesbírat (import selže dřív, než se stihne cokoliv testovat).
Spouštět je jde jen na Linuxu/macOS — lokálně přes WSL, v CI běží na
`ubuntu-latest` (viz `.github/workflows/validate.yml`), takže tam to
funguje bez omezení.
"""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


# `recorder_mock` MUSÍ být prvním parametrem (a tedy resolvnutý první) —
# interně přes `recorder_db_url` kontroluje, že `hass` ještě neběžel, jinak
# AssertionError. Kdyby byl `enable_custom_integrations` (závislý na `hass`)
# ve vlastním samostatném autouse fixture, hass by se inicializoval dřív a
# recorder_mock by pak spadl. Musí to tedy být jeden fixture, v tomto pořadí
# parametrů.
@pytest.fixture(autouse=True)
def auto_ha_fixtures(recorder_mock, enable_custom_integrations):
    """`manifest.json` má `dependencies: ["recorder"]` (external statistics) —
    bez in-memory recorderu z `pytest-homeassistant-custom-component` selže
    načtení integrace (i jen pro config/options flow) na `DependencyError`.
    """
    yield
