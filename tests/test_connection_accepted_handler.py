"""Tests fuer den connection_accepted-Handler aus __init__.py.

Schwerpunkt: Phase A (requestId-bewusste Idempotenz) und Phase D
(Slug-Weitergabe an connect_for_tunnel). Der Handler ist eine Closure in
``__init__.py``; das Modul wird vom conftest bewusst NICHT als Package
ausgefuehrt (zu viele HA-Imports), darum laden wir es hier gezielt nach.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_init_module():
    """Laedt das echte ha_fleet_agent/__init__.py mit den conftest-Stubs nach."""
    mod = sys.modules.get("ha_fleet_agent")
    if mod is not None and hasattr(mod, "_make_connection_accepted_handler"):
        return mod
    init_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "ha_fleet_agent"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(
        "ha_fleet_agent",
        init_path,
        submodule_search_locations=[str(init_path.parent)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ha_fleet_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


_init = _load_init_module()
_make_connection_accepted_handler = _init._make_connection_accepted_handler


# --------------------------------------------------------- Stubs


class FakeWsClient:
    def __init__(self, *, connected: bool = False):
        self.is_connected = connected
        self.disconnect_calls = 0
        self.connect_calls: list[dict[str, Any]] = []
        self.fail_connect = False

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def connect_for_tunnel(
        self, tunnel_token: str, connector_url: str, slug: str | None = None
    ) -> None:
        self.connect_calls.append(
            {"token": tunnel_token, "url": connector_url, "slug": slug}
        )
        if self.fail_connect:
            raise RuntimeError("connect failed")
        self.is_connected = True


class FakeTunnelForwarder:
    def __init__(self, active_request_id: str | None = None):
        self._active_request_id = active_request_id
        self.token: str | None = None
        self.handover_calls = 0

    def set_active_tunnel_token(self, token: str) -> None:
        self.token = token

    def set_active_request_id(self, request_id: str | None) -> None:
        self._active_request_id = request_id or None

    @property
    def active_request_id(self) -> str | None:
        return self._active_request_id

    def mark_handover_close(self) -> None:
        self.handover_calls += 1


class FakeRemoteAccess:
    def __init__(self):
        self.idle_calls = 0

    async def _on_poll_idle(self, _data: Any = None) -> None:
        self.idle_calls += 1


def _make(ws_client, forwarder, remote_access=None, relay_url="wss://relay.test/ws/agent"):
    return _make_connection_accepted_handler(
        hass=object(),
        entry_id="e1",
        ws_client=ws_client,
        tunnel_forwarder=forwarder,
        relay_url=relay_url,
        remote_access=remote_access or FakeRemoteAccess(),
    )


def _frame(**kwargs) -> dict[str, Any]:
    base = {
        "action": "connection_accepted",
        "requestId": "req-1",
        "tunnelToken": "tt-abc",
        "connectorUrl": "wss://relay.test/ws/agent?token=tt-abc",
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------- Tests Phase A


@pytest.mark.asyncio
async def test_baut_auf_wenn_keine_ws_aktiv():
    ws = FakeWsClient(connected=False)
    fwd = FakeTunnelForwarder()
    handler = _make(ws, fwd)

    await handler(_frame())

    assert len(ws.connect_calls) == 1
    assert ws.disconnect_calls == 0
    assert fwd.token == "tt-abc"
    assert fwd.active_request_id == "req-1"


@pytest.mark.asyncio
async def test_ignoriert_duplikat_fuer_laufende_request_id():
    """Backend-Neustart-Fall: WS laeuft, Backend liefert fuer DIESELBE Anfrage
    erneut einen Token → kein Neuaufbau, kein Disconnect."""
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    handler = _make(ws, fwd)

    await handler(_frame(requestId="req-1"))

    assert ws.connect_calls == []
    assert ws.disconnect_calls == 0
    # Der laufende Zustand darf nicht ueberschrieben werden.
    assert fwd.active_request_id == "req-1"


@pytest.mark.asyncio
async def test_neue_request_id_schliesst_alten_tunnel_und_baut_neu_auf():
    """Echte neue Anfrage: WS laeuft mit req-1, jetzt kommt req-2 →
    alten Tunnel schliessen, neu aufbauen."""
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    handler = _make(ws, fwd)

    await handler(_frame(requestId="req-2"))

    assert ws.disconnect_calls == 1
    assert len(ws.connect_calls) == 1
    assert fwd.active_request_id == "req-2"
    # Der alte Tunnel-Close muss als Handover markiert sein (kein Reconnect/Session-Ende).
    assert fwd.handover_calls == 1


@pytest.mark.asyncio
async def test_ohne_tunnel_token_wird_ignoriert():
    ws = FakeWsClient(connected=False)
    fwd = FakeTunnelForwarder()
    handler = _make(ws, fwd)

    await handler(_frame(tunnelToken=""))

    assert ws.connect_calls == []


@pytest.mark.asyncio
async def test_connect_fehler_setzt_zustand_zurueck():
    ws = FakeWsClient(connected=False)
    ws.fail_connect = True
    fwd = FakeTunnelForwarder()
    handler = _make(ws, fwd)

    await handler(_frame())

    # Nach Fehlschlag darf kein verwaister Zustand zurueckbleiben.
    assert fwd.token == ""
    assert fwd.active_request_id is None


# --------------------------------------------------------- Tests Phase D


@pytest.mark.asyncio
async def test_slug_wird_an_connect_weitergereicht():
    ws = FakeWsClient(connected=False)
    fwd = FakeTunnelForwarder()
    handler = _make(ws, fwd)

    await handler(_frame(slug="deadbeef"))

    assert ws.connect_calls[0]["slug"] == "deadbeef"


@pytest.mark.asyncio
async def test_ohne_slug_wird_leerer_slug_weitergereicht():
    """Aelteres Backend liefert keinen Slug → connect bekommt leeren Slug,
    der Connector wuerfelt dann (Backward-Compat)."""
    ws = FakeWsClient(connected=False)
    fwd = FakeTunnelForwarder()
    handler = _make(ws, fwd)

    await handler(_frame())  # kein slug-Feld

    assert ws.connect_calls[0]["slug"] == ""
