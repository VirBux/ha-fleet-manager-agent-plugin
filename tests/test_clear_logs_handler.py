"""Tests fuer den ClearLogsHandler (clear_logs-Poll-Aktion, #109)."""

from __future__ import annotations

import pytest

from ha_fleet_agent.clear_logs_handler import ClearLogsHandler


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
            raise RuntimeError("system_log.clear failed")


class FakeHass:
    def __init__(self, fail: bool = False):
        self.services = FakeServices(fail)


class FakeStateReporter:
    """state_reporter-Stub: zaehlt push_now-Aufrufe, kann gezielt werfen."""

    def __init__(self, fail: bool = False):
        self.push_count = 0
        self._fail = fail

    async def push_now(self) -> None:
        self.push_count += 1
        if self._fail:
            raise RuntimeError("push failed")


# --------------------------------------------------------- Tests


@pytest.mark.asyncio
async def test_clear_ruft_system_log_clear_und_pusht():
    """handle ruft system_log.clear (blocking) und stoesst danach einen State-Push an."""
    hass = FakeHass()
    reporter = FakeStateReporter()
    await ClearLogsHandler(hass, reporter).handle({"action": "clear_logs"})

    assert len(hass.services.calls) == 1
    call = hass.services.calls[0]
    assert call["domain"] == "system_log"
    assert call["service"] == "clear"
    assert call["data"] == {}
    assert call["blocking"] is True  # schnell + Fehlerergebnis direkt
    assert reporter.push_count == 1  # Sofort-Push nach erfolgreichem clear


@pytest.mark.asyncio
async def test_service_fehler_kein_crash_und_kein_push():
    """Faellt system_log.clear aus, wird nur geloggt — kein Crash, kein Sofort-Push."""
    hass = FakeHass(fail=True)
    reporter = FakeStateReporter()
    await ClearLogsHandler(hass, reporter).handle({"action": "clear_logs"})

    assert len(hass.services.calls) == 1  # wurde versucht
    assert reporter.push_count == 0  # nach Fehler kein Push (early return)


@pytest.mark.asyncio
async def test_push_fehler_kein_crash():
    """Ein fehlschlagender Sofort-Push darf den Handler nicht crashen (naechster Tick zieht nach)."""
    hass = FakeHass()
    reporter = FakeStateReporter(fail=True)
    await ClearLogsHandler(hass, reporter).handle({"action": "clear_logs"})  # darf nicht werfen

    assert hass.services.calls[0]["service"] == "clear"
    assert reporter.push_count == 1  # wurde versucht


@pytest.mark.asyncio
async def test_handle_toleriert_leere_data():
    """Die Aktion traegt keine Nutzdaten — leeres data ist ok."""
    hass = FakeHass()
    reporter = FakeStateReporter()
    await ClearLogsHandler(hass, reporter).handle({})
    assert len(hass.services.calls) == 1
