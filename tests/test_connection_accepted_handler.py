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
        self.rebuild_calls = 0
        self.close_calls = 0

    def set_active_tunnel_token(self, token: str) -> None:
        self.token = token

    def set_active_request_id(self, request_id: str | None) -> None:
        self._active_request_id = request_id or None

    @property
    def active_request_id(self) -> str | None:
        return self._active_request_id

    def mark_handover_close(self) -> None:
        self.handover_calls += 1

    def mark_rebuild_close(self) -> None:
        self.rebuild_calls += 1

    async def async_close_tunnel(self) -> bool:
        self.close_calls += 1
        return True


class FakeRemoteAccess:
    def __init__(self, *, session_ok: bool = True):
        self.idle_calls = 0
        self.session_ok = session_ok
        self.ensure_calls: list[dict[str, Any]] = []
        # Wie RemoteAccessManager.session: die laufende Session (nur request_id nötig).
        self.session: Any = None

    def end_session(self) -> None:
        self.session = None

    async def _on_poll_idle(self, _data: Any = None) -> None:
        self.idle_calls += 1

    async def async_ensure_session_for_accepted(
        self, request_id: str, *, subject: str = "", session_expires_at=None
    ) -> bool:
        self.ensure_calls.append(
            {
                "request_id": request_id,
                "subject": subject,
                "session_expires_at": session_expires_at,
            }
        )
        if self.session_ok:
            from types import SimpleNamespace

            self.session = SimpleNamespace(request_id=request_id)
        return self.session_ok


class FakeClock:
    """Steuerbare Monotonic-Uhr für die Neuaufbau-Bremse."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _make(
    ws_client,
    forwarder,
    remote_access=None,
    relay_url="wss://relay.test/ws/agent",
    *,
    clock=None,
    settle_s: float = 0.0,
):
    return _make_connection_accepted_handler(
        hass=object(),
        entry_id="e1",
        ws_client=ws_client,
        tunnel_forwarder=forwarder,
        relay_url=relay_url,
        remote_access=remote_access or FakeRemoteAccess(),
        rebuild_settle_s=settle_s,
        monotonic=clock or FakeClock(),
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
async def test_erneutes_accepted_für_laufende_request_id_baut_tunnel_neu_auf():
    """WS läuft, Backend liefert für DIESELBE Anfrage erneut einen Token.

    Das Backend gibt nur dann einen Token aus, wenn es keinen Live-Tunnel kennt
    (Zugangsdaten nie angekommen, Backend-Cache nach Neustart leer). Früher wurde
    der Frame ignoriert — dann hing der Zustand dauerhaft: alle 15 s ein neuer
    Token, im Frontend „Session aktiv, keine Live-Verbindung" (Staging 2026-09-23).
    Jetzt: Neuaufbau mit dem frischen Token, als REBUILD markiert (kein DELETE,
    der die Anfrage im Backend schließen würde)."""
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    handler = _make(ws, fwd)

    await handler(_frame(requestId="req-1", tunnelToken="tt-neu"))

    assert ws.disconnect_calls == 1
    assert len(ws.connect_calls) == 1
    assert ws.connect_calls[0]["token"] == "tt-neu"
    assert fwd.token == "tt-neu"
    assert fwd.active_request_id == "req-1"
    assert fwd.rebuild_calls == 1
    assert fwd.handover_calls == 0, "Neuaufbau ist kein Handover (kein DELETE)"


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


@pytest.mark.asyncio
async def test_wiederholter_neuaufbau_derselben_anfrage_wird_gebremst():
    """Kommen die Zugangsdaten dauerhaft nicht an, liefert das Backend alle 15 s
    erneut connection_accepted. Nicht jedes Mal neu aufbauen: 15 s, 30 s, 60 s …"""
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    clock = FakeClock()
    handler = _make(ws, fwd, clock=clock)

    await handler(_frame(requestId="req-1"))  # 1. Neuaufbau sofort
    assert fwd.rebuild_calls == 1

    clock.now += 10
    await handler(_frame(requestId="req-1"))  # < 15 s → gebremst
    assert fwd.rebuild_calls == 1
    assert len(ws.connect_calls) == 1

    clock.now += 6
    await handler(_frame(requestId="req-1"))  # > 15 s → 2. Neuaufbau
    assert fwd.rebuild_calls == 2

    clock.now += 20
    await handler(_frame(requestId="req-1"))  # < 30 s → gebremst
    assert fwd.rebuild_calls == 2

    clock.now += 11
    await handler(_frame(requestId="req-1"))  # > 30 s → 3. Neuaufbau
    assert fwd.rebuild_calls == 3


@pytest.mark.asyncio
async def test_neuaufbau_bremse_ist_auf_fuenf_minuten_gedeckelt():
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    clock = FakeClock()
    handler = _make(ws, fwd, clock=clock)

    for _ in range(10):  # viele Neuaufbauten, je 400 s: über dem Deckel, unter dem Reset
        await handler(_frame(requestId="req-1"))
        clock.now += 400
    assert fwd.rebuild_calls == 10

    await handler(_frame(requestId="req-1"))  # Nr. 11
    clock.now += 299
    await handler(_frame(requestId="req-1"))  # < 300 s → gebremst
    assert fwd.rebuild_calls == 11
    clock.now += 2
    await handler(_frame(requestId="req-1"))  # > 300 s → erlaubt, nicht 2^n
    assert fwd.rebuild_calls == 12


@pytest.mark.asyncio
async def test_neuaufbau_bremse_beginnt_fuer_neue_anfrage_von_vorn():
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    clock = FakeClock()
    handler = _make(ws, fwd, clock=clock)
    await handler(_frame(requestId="req-1"))
    clock.now += 16
    await handler(_frame(requestId="req-1"))  # Zähler für req-1 steht auf 2
    await handler(_frame(requestId="req-2"))  # Handover
    assert fwd.active_request_id == "req-2"

    await handler(_frame(requestId="req-2"))  # 1. Neuaufbau für req-2: sofort

    assert fwd.rebuild_calls == 3


@pytest.mark.asyncio
async def test_neuaufbau_wartet_vor_dem_connect(monkeypatch):
    """Pause zwischen Close und Connect: Der Close-Notify des alten Tunnels (gleicher
    Slug) soll vor dem Credentials-POST des neuen beim Backend sein."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(_init.asyncio, "sleep", fake_sleep)
    ws = FakeWsClient(connected=True)
    handler = _make(ws, FakeTunnelForwarder(active_request_id="req-1"), settle_s=2.0)

    await handler(_frame(requestId="req-1"))

    assert sleeps == [2.0]
    assert len(ws.connect_calls) == 1


@pytest.mark.asyncio
async def test_handover_auf_neue_anfrage_wartet_nicht(monkeypatch):
    """Neue Anfrage = neuer Slug, kein Rennen — keine Pause nötig."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(_init.asyncio, "sleep", fake_sleep)
    ws = FakeWsClient(connected=True)
    handler = _make(ws, FakeTunnelForwarder(active_request_id="req-1"), settle_s=2.0)

    await handler(_frame(requestId="req-2"))

    assert sleeps == []


@pytest.mark.asyncio
async def test_session_ende_in_der_neuaufbau_pause_verhindert_den_connect(monkeypatch):
    """Endet die Session in der 2-s-Pause (Ablauf, Ablehnung), ist async_close_tunnel
    ein No-op, weil noch keine WS steht — der Handler darf dann nicht verbinden."""
    ra = FakeRemoteAccess()

    async def fake_sleep(_seconds):
        ra.end_session()

    monkeypatch.setattr(_init.asyncio, "sleep", fake_sleep)
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    handler = _make(ws, fwd, remote_access=ra, settle_s=2.0)

    await handler(_frame(requestId="req-1"))

    assert ws.connect_calls == []


@pytest.mark.asyncio
async def test_session_ende_waehrend_des_handshakes_schliesst_den_tunnel():
    ra = FakeRemoteAccess()

    class EndingWsClient(FakeWsClient):
        async def connect_for_tunnel(self, tunnel_token, connector_url, slug=None):
            await super().connect_for_tunnel(tunnel_token, connector_url, slug=slug)
            ra.end_session()  # Ablauf/Ablehnung während des Handshakes

    ws = EndingWsClient(connected=False)
    fwd = FakeTunnelForwarder()
    handler = _make(ws, fwd, remote_access=ra)

    await handler(_frame(requestId="req-1"))

    assert len(ws.connect_calls) == 1
    assert fwd.close_calls == 1


@pytest.mark.asyncio
async def test_ohne_freigabe_wird_offene_ws_derselben_anfrage_geschlossen():
    """Beendete Anfrage, deren Tunnel noch steht (etwa weil das Session-Ende in den
    Aufbau fiel und das Close ans Backend scheiterte): beim nächsten Poll schließen."""
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    handler = _make(ws, fwd, remote_access=FakeRemoteAccess(session_ok=False))

    await handler(_frame(requestId="req-1"))

    assert fwd.close_calls == 1
    assert ws.connect_calls == []


@pytest.mark.asyncio
async def test_ohne_freigabe_bleibt_tunnel_einer_anderen_anfrage_stehen():
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    handler = _make(ws, fwd, remote_access=FakeRemoteAccess(session_ok=False))

    await handler(_frame(requestId="req-2"))

    assert fwd.close_calls == 0


@pytest.mark.asyncio
async def test_neuaufbau_bremse_setzt_nach_langer_ruhe_zurueck():
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    clock = FakeClock()
    handler = _make(ws, fwd, clock=clock)
    for _ in range(4):
        await handler(_frame(requestId="req-1"))
        clock.now += 10_000  # jeweils > Pause, Zähler steigt
    # Lange ruhig: Die nächsten zwei Neuaufbauten folgen wieder der ersten Stufe.
    await handler(_frame(requestId="req-1"))
    clock.now += 16
    await handler(_frame(requestId="req-1"))

    assert fwd.rebuild_calls == 6


# --------------------------------------------------------- Tests Session-Resume


@pytest.mark.asyncio
async def test_stellt_session_vor_dem_tunnel_sicher():
    """Vorab-Freigabe: Das Plugin kennt die Annahme nur aus connection_accepted.
    Der Handler richtet die Session (samt Wartungs-User) ein, bevor er verbindet —
    mit Betreff und serverseitigem Session-Ende aus dem Frame."""
    ws = FakeWsClient(connected=False)
    fwd = FakeTunnelForwarder()
    ra = FakeRemoteAccess()
    handler = _make(ws, fwd, remote_access=ra)

    await handler(
        _frame(subject="Heizung prüfen", sessionExpiresAt="2026-09-23T12:47:46.966482Z")
    )

    assert len(ra.ensure_calls) == 1
    call = ra.ensure_calls[0]
    assert call["request_id"] == "req-1"
    assert call["subject"] == "Heizung prüfen"
    expires = call["session_expires_at"]
    assert expires is not None and expires.tzinfo is not None
    assert (expires.year, expires.hour, expires.minute) == (2026, 12, 47)
    assert len(ws.connect_calls) == 1


@pytest.mark.asyncio
async def test_ohne_session_ende_im_frame_wird_none_weitergereicht():
    """Älteres Backend ohne sessionExpiresAt → None; die Session fällt dann
    auf die Dauer der Vorab-Freigabe bzw. das Maximum zurück."""
    ws = FakeWsClient(connected=False)
    ra = FakeRemoteAccess()
    handler = _make(ws, FakeTunnelForwarder(), remote_access=ra)

    await handler(_frame())

    assert ra.ensure_calls[0]["session_expires_at"] is None


@pytest.mark.asyncio
async def test_kein_tunnel_wenn_wartungs_user_nicht_aktivierbar():
    """Fail-closed (#110): Ist der Wartungs-User nicht aktivierbar (z.B. vom
    Endkunden gelöscht), wird kein Tunnel aufgebaut — auch kein laufender
    Tunnel angefasst."""
    ws = FakeWsClient(connected=True)
    fwd = FakeTunnelForwarder(active_request_id="req-1")
    ra = FakeRemoteAccess(session_ok=False)
    handler = _make(ws, fwd, remote_access=ra)

    await handler(_frame(requestId="req-2"))

    assert ws.connect_calls == []
    assert ws.disconnect_calls == 0
    assert fwd.active_request_id == "req-1"


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



# --------------------------------------------------------- Unload (#165)


@pytest.mark.asyncio
async def test_unload_beendet_session_vor_dem_abbau():
    """Unload ist ein gewolltes Ende: Session (samt fortsetzbarer) beenden, BEVOR
    Forwarder und HTTP-Session abgebaut werden — sonst ginge der Close ans Backend
    nicht mehr raus."""
    order: list[str] = []

    class _Recorder:
        def __init__(self, name: str):
            self._name = name

        def __getattr__(self, attr):
            async def _async(*_a, **_kw):
                order.append(f"{self._name}.{attr}")

            def _sync(*_a, **_kw):
                order.append(f"{self._name}.{attr}")

            return _sync if attr in ("stop", "cancel") else _async

    class _HttpSession:
        closed = False

        async def close(self):
            order.append("http_session.close")

    class _ConfigEntries:
        async def async_unload_platforms(self, _entry, _platforms):
            return True

    class _Hass:
        def __init__(self):
            self.config_entries = _ConfigEntries()
            self.data = {}

    class _Entry:
        entry_id = "e1"
        options: dict = {}

    hass = _Hass()
    hass.data[_init.DOMAIN] = {
        "e1": {
            _init.DATA_STATE_REPORTER: _Recorder("state_reporter"),
            _init.DATA_REQUEST_POLLER: _Recorder("request_poller"),
            _init.DATA_RECONNECTOR: _Recorder("reconnector"),
            _init.DATA_REMOTE_ACCESS: _Recorder("remote_access"),
            _init.DATA_TUNNEL_FORWARDER: _Recorder("tunnel_forwarder"),
            _init.DATA_CLIENT: _Recorder("ws_client"),
            _init.DATA_INTEGRATOR_USER: _Recorder("integrator_user"),
            _init.DATA_HTTP_SESSION: _HttpSession(),
        }
    }

    assert await _init.async_unload_entry(hass, _Entry()) is True

    assert order.index("request_poller.stop") < order.index("remote_access.async_end_for_unload")
    assert order.index("remote_access.async_end_for_unload") < order.index(
        "remote_access.async_shutdown"
    )
    assert order.index("remote_access.async_shutdown") < order.index(
        "tunnel_forwarder.async_shutdown"
    )
    assert order[-1] == "http_session.close"
