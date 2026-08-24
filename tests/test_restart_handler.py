"""Tests fuer den RestartHandler (restart-Poll-Aktion, #127 Sofort-Weg)."""

from __future__ import annotations

import pytest

from ha_fleet_agent.restart_handler import RestartHandler


# --------------------------------------------------------- Stubs


class FakeServices:
    """hass.services-Stub: zeichnet async_call-Aufrufe auf, kann gezielt werfen."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    async def async_call(
        self, domain, service, service_data=None, blocking=False
    ):  # noqa: ANN001
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "data": dict(service_data or {}),
                "blocking": blocking,
            }
        )
        if self._fail:
            raise RuntimeError("homeassistant.restart failed")


class FakeHass:
    def __init__(self, fail: bool = False):
        self.services = FakeServices(fail)


# --------------------------------------------------------- Tests


@pytest.mark.asyncio
async def test_restart_ruft_homeassistant_restart_nicht_blockierend():
    """handle ruft homeassistant.restart — bewusst blocking=False (der Prozess endet)."""
    hass = FakeHass()
    await RestartHandler(hass).handle({"action": "restart"})

    assert len(hass.services.calls) == 1
    call = hass.services.calls[0]
    assert call["domain"] == "homeassistant"
    assert call["service"] == "restart"
    assert call["data"] == {}
    assert call["blocking"] is False


@pytest.mark.asyncio
async def test_service_fehler_kein_crash():
    """Faellt der Service-Aufruf aus, wird nur geloggt — der Poll darf nicht crashen."""
    hass = FakeHass(fail=True)
    await RestartHandler(hass).handle({"action": "restart"})  # darf nicht werfen

    assert len(hass.services.calls) == 1  # wurde versucht


@pytest.mark.asyncio
async def test_handle_toleriert_leere_data():
    """Die Aktion traegt keine Nutzdaten — ein leeres dict genuegt."""
    hass = FakeHass()
    await RestartHandler(hass).handle({})

    assert hass.services.calls[0]["service"] == "restart"
