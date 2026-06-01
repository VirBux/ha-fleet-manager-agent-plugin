"""Tests fuer den RequestPoller (REST, 15 s, TODO #50.20)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ha_fleet_agent.request_poller import RequestPoller


# --------------------------------------------------------- Stubs


class _FakeResponse:
    def __init__(self, status: int, json_data: dict | None = None):
        self.status = status
        self._json = json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self) -> dict:
        if self._json is None:
            raise ValueError("Kein JSON")
        return self._json


class FakeSession:
    """Stub fuer aiohttp.ClientSession — GET-Requests."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}})
        return self._response


class FakeHass:
    def __init__(self):
        self._tasks = []

    def async_create_task(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task


# --------------------------------------------------------- Tests


@pytest.mark.asyncio
async def test_204_tut_nichts():
    """HTTP 204 — kein Handler aufgerufen."""
    session = FakeSession(_FakeResponse(204))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    called = []
    poller.register_handler("connection_request", lambda d: called.append(d))

    await poller._poll_once()

    assert called == []
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_200_connection_request_dispatcht_handler():
    """HTTP 200 mit action=connection_request → Handler aufgerufen."""
    payload = {
        "action": "connection_request",
        "request_id": "req-1",
        "subject": "Testperson",
        "reason": "Diagnose",
        "duration_hours": 2,
    }
    session = FakeSession(_FakeResponse(200, payload))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    received: list[dict] = []

    async def handler(data: dict) -> None:
        received.append(data)

    poller.register_handler("connection_request", handler)

    await poller._poll_once()

    assert len(received) == 1
    assert received[0]["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_200_connection_accepted_dispatcht_handler():
    """HTTP 200 mit action=connection_accepted → entsprechender Handler."""
    payload = {
        "action": "connection_accepted",
        "tunnelToken": "tok-abc",
        "connectorUrl": "wss://relay.ha-fleet-manager.com/ws/tunnel?token=tok-abc",
    }
    session = FakeSession(_FakeResponse(200, payload))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    received: list[dict] = []

    async def handler(data: dict) -> None:
        received.append(data)

    poller.register_handler("connection_accepted", handler)

    await poller._poll_once()

    assert len(received) == 1
    assert received[0]["tunnelToken"] == "tok-abc"


@pytest.mark.asyncio
async def test_200_unbekannte_action_kein_crash():
    """Unbekannte action → Debug-Log, kein Crash."""
    payload = {"action": "future_feature_xyz"}
    session = FakeSession(_FakeResponse(200, payload))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    # Kein Handler registriert — darf nicht crashen
    await poller._poll_once()


@pytest.mark.asyncio
async def test_401_kein_crash():
    """401-Antwort — nur loggen, weiter pollen."""
    session = FakeSession(_FakeResponse(401))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    await poller._poll_once()  # Darf keinen Exception werfen


@pytest.mark.asyncio
async def test_timeout_kein_crash():
    """Timeout soll abgefangen werden."""

    class _TimeoutSession:
        calls: list = []

        def get(self, url, headers=None, timeout=None):
            class _Ctx:
                async def __aenter__(self):
                    raise asyncio.TimeoutError()

                async def __aexit__(self, *_):
                    return False

            return _Ctx()

    hass = FakeHass()
    poller = RequestPoller(hass, _TimeoutSession(), "https://api.ha-fleet-manager.com", "key")

    await poller._poll_once()  # Darf keinen Exception werfen


@pytest.mark.asyncio
async def test_poll_endpoint_url_korrekt():
    """Poller muss /api/agent/poll aufrufen."""
    session = FakeSession(_FakeResponse(204))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key123")

    await poller._poll_once()

    assert session.calls[0]["url"] == "https://api.ha-fleet-manager.com/api/agent/poll"
    assert session.calls[0]["headers"]["X-API-Key"] == "key123"


@pytest.mark.asyncio
async def test_200_ohne_action_feld_kein_crash():
    """Payload ohne 'action'-Feld → Warnung, kein Crash."""
    payload = {"some": "data"}
    session = FakeSession(_FakeResponse(200, payload))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    await poller._poll_once()  # Darf keinen Exception werfen


@pytest.mark.asyncio
async def test_204_dispatcht_idle_action():
    """HTTP 204 → synthetische 'idle'-Aktion (#90, Self-Healing-Hook).

    Damit koennen Handler verwaiste Zustaende aufraeumen (z.B. das Repair-Issue
    einer abgebrochenen/abgelaufenen Verbindungsanfrage).
    """
    session = FakeSession(_FakeResponse(204))
    hass = FakeHass()
    poller = RequestPoller(hass, session, "https://api.ha-fleet-manager.com", "key")

    idle_calls: list[dict] = []

    async def idle_handler(data: dict) -> None:
        idle_calls.append(data)

    poller.register_handler("idle", idle_handler)

    await poller._poll_once()

    assert len(idle_calls) == 1
    assert idle_calls[0]["action"] == "idle"
