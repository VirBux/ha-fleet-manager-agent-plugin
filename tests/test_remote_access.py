"""Tests fuer RemoteAccessManager — REST-Calls statt WS-Frames (Phase 4 #50.23)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ha_fleet_agent.remote_access import RemoteAccessManager


# --------------------------------------------------------- Stubs


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    """Zeichnet alle POST- und DELETE-Calls auf."""

    def __init__(self, response_status: int = 204):
        self._status = response_status
        self.calls: list[dict] = []

    def _ctx(self, call: dict):
        self.calls.append(call)
        return _FakeResponse(self._status)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._ctx({"method": "POST", "url": url, "json": json, "headers": headers or {}})

    def delete(self, url, headers=None, timeout=None):
        return self._ctx({"method": "DELETE", "url": url, "headers": headers or {}})


class FakeHass:
    """Minimal-Stub mit Services und async_create_task."""

    def __init__(self):
        self._tasks = []
        self.services = _FakeServices()

    def async_create_task(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task

    async def run_tasks(self):
        await asyncio.gather(*self._tasks, return_exceptions=True)


class _FakeServices:
    def __init__(self):
        self.calls: list[dict] = []

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append({"domain": domain, "service": service, "data": data})


class _FakeCreds:
    """Minimal-Credentials-Stub (#110)."""

    def __init__(self, error: str | None = None):
        self.error = error
        self.active = error is None
        self.username = "ha-fleet-integrator"
        self.password = "" if error else "rotated-pw"


class FakeIntegratorUser:
    """Stub fuer IntegratorUserManager (#110): zaehlt activate/deactivate auf.

    ``mode``: "ok" → erfolgreiche Aktivierung; "error" → Credentials mit error;
    "none" → None (kein Credentials-Objekt).
    """

    def __init__(self, mode: str = "ok"):
        self._mode = mode
        self.activated = 0
        self.deactivated = 0
        self.kept_tokens: list[bool] = []  # je Aktivierung: Tokens behalten (#165)

    async def async_activate(self, *, remove_stale_tokens: bool = True):
        self.activated += 1
        self.kept_tokens.append(not remove_stale_tokens)
        if self._mode == "none":
            return None
        if self._mode == "error":
            return _FakeCreds(error="user_missing")
        return _FakeCreds()

    async def async_deactivate(self):
        self.deactivated += 1


def make_manager(
    session: FakeSession, response_status: int = 204, integrator_user: Any = None
) -> RemoteAccessManager:
    hass = FakeHass()
    mgr = RemoteAccessManager(
        hass,
        entry_id="test-entry",
        session=session,
        backend_url="https://api.ha-fleet-manager.com",
        api_key="test-api-key",
        integrator_user=integrator_user,
    )
    return mgr


# --------------------------------------------------------- Tests: confirm_request


@pytest.mark.asyncio
async def test_confirm_request_accept_sendet_rest_post():
    """Anfrage akzeptieren → POST /api/agent/connection-requests/{id}/accept."""
    session = FakeSession(204)
    mgr = make_manager(session)

    await mgr.confirm_request("req-1", accepted=True, duration_hours=2)

    post_calls = [c for c in session.calls if c["method"] == "POST"]
    assert len(post_calls) >= 1

    accept_call = next(
        (c for c in post_calls if "/connection-requests/req-1/accept" in c["url"]),
        None,
    )
    assert accept_call is not None, "Kein Accept-POST gefunden"
    assert accept_call["json"]["duration_hours"] == 2
    assert accept_call["headers"]["X-API-Key"] == "test-api-key"


@pytest.mark.asyncio
async def test_confirm_request_reject_sendet_rest_post():
    """Anfrage ablehnen → POST /api/agent/connection-requests/{id}/reject."""
    session = FakeSession(204)
    mgr = make_manager(session)

    await mgr.confirm_request("req-2", accepted=False)

    reject_call = next(
        (c for c in session.calls if "/connection-requests/req-2/reject" in c["url"]),
        None,
    )
    assert reject_call is not None, "Kein Reject-POST gefunden"
    assert reject_call["headers"]["X-API-Key"] == "test-api-key"


@pytest.mark.asyncio
async def test_confirm_request_leer_tut_nichts():
    """Leere request_id → kein REST-Call."""
    session = FakeSession()
    mgr = make_manager(session)

    await mgr.confirm_request("", accepted=True)

    assert session.calls == []


# --------------------------------------------------------- Tests: _announce_preauth


@pytest.mark.asyncio
async def test_announce_preauth_mit_aktiver_preauth_postet():
    """Aktive Pre-Auth → POST /api/agent/preauth mit expires_at und max_duration_hours."""
    import datetime

    from ha_fleet_agent.remote_access import PreAuthorization
    from homeassistant.util import dt as dt_util  # noqa: PLC0415 — Stub aus conftest

    session = FakeSession(200)
    mgr = make_manager(session)

    expires = dt_util.utcnow() + datetime.timedelta(hours=4)
    mgr._pre_auth = PreAuthorization(expires_at=expires, max_duration_hours=2)

    await mgr._announce_preauth()

    post_calls = [c for c in session.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert "/api/agent/preauth" in post_calls[0]["url"]
    body = post_calls[0]["json"]
    assert "expires_at" in body
    assert body["max_duration_hours"] == 2


@pytest.mark.asyncio
async def test_announce_preauth_ohne_preauth_sendet_delete():
    """Keine Pre-Auth → DELETE /api/agent/preauth."""
    session = FakeSession(204)
    mgr = make_manager(session)
    mgr._pre_auth = None

    await mgr._announce_preauth()

    delete_calls = [c for c in session.calls if c["method"] == "DELETE"]
    assert len(delete_calls) == 1
    assert "/api/agent/preauth" in delete_calls[0]["url"]


@pytest.mark.asyncio
async def test_rest_fehler_kein_crash():
    """Backend-Fehler bei confirm_request soll keinen Exception werfen."""
    session = FakeSession(500)  # Server-Fehler
    mgr = make_manager(session)

    # Darf nicht crashen
    await mgr.confirm_request("req-3", accepted=True, duration_hours=1)


# --------------------------------------------------------- Tests: _on_connection_request


@pytest.mark.asyncio
async def test_on_connection_request_ohne_preauth_erzeugt_repair_issue():
    """Ohne Pre-Auth → Repair-Issue (ir.async_create_issue) wird erstellt.

    Endkunde sieht es als gelben Banner auf dem HA-Dashboard und kann den
    Repair-Flow (siehe repairs.py) zum Annehmen/Ablehnen öffnen.
    """
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    data = {
        "request_id": "req-42",
        "subject": "Denny Test",
        "reason": "Diagnose",
        "duration_hours": 2,
    }
    await mgr._on_connection_request(data)

    create_calls = [
        c for c in ir._test_calls if c["action"] == "create"  # type: ignore[attr-defined]
    ]
    assert len(create_calls) == 1
    issue = create_calls[0]
    assert issue["domain"] == "ha_fleet_agent"
    assert issue["issue_id"] == "connection_request_req-42"
    assert issue["is_fixable"] is True
    assert issue["translation_key"] == "connection_request"
    # Daten werden im Issue persistiert → Repair-Flow kann sie auslesen
    assert issue["data"]["request_id"] == "req-42"
    assert issue["data"]["subject"] == "Denny Test"
    assert issue["data"]["reason"] == "Diagnose"
    assert issue["data"]["duration_hours"] == 2
    assert issue["data"]["entry_id"] == "test-entry"


@pytest.mark.asyncio
async def test_on_connection_request_liest_camelcase_vom_backend():
    """Quarkus liefert camelCase (requestId, duration) — Plugin muss das verstehen.

    Regression: Vor Fix las das Plugin nur snake_case → requestId leer →
    Accept-POST ging an .../connection-requests//accept → 404, Anfrage
    kam beim nächsten Poll erneut.
    """
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    data = {
        "action": "connection_request",
        "requestId": "11111111-2222-3333-4444-555555555555",
        "subject": "Heizung",
        "duration": 4,
        "reason": "Diagnose",
    }
    await mgr._on_connection_request(data)

    create_calls = [
        c for c in ir._test_calls if c["action"] == "create"  # type: ignore[attr-defined]
    ]
    assert len(create_calls) == 1
    issue = create_calls[0]
    assert issue["issue_id"] == "connection_request_11111111-2222-3333-4444-555555555555"
    assert issue["data"]["request_id"] == "11111111-2222-3333-4444-555555555555"
    assert issue["data"]["duration_hours"] == 4
    assert issue["translation_placeholders"]["requested_hours"] == "4"


@pytest.mark.asyncio
async def test_on_connection_request_ohne_request_id_ignoriert():
    """Defensiv: kein requestId → kein Issue, kein Crash."""
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    await mgr._on_connection_request({"subject": "Test"})

    create_calls = [
        c for c in ir._test_calls if c["action"] == "create"  # type: ignore[attr-defined]
    ]
    assert create_calls == []


@pytest.mark.asyncio
async def test_confirm_request_loescht_repair_issue():
    """Nach confirm_request → Issue wird via ir.async_delete_issue entfernt."""
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession(204)
    mgr = make_manager(session)

    await mgr.confirm_request("req-7", accepted=True, duration_hours=2)

    delete_calls = [
        c for c in ir._test_calls if c["action"] == "delete"  # type: ignore[attr-defined]
    ]
    assert len(delete_calls) == 1
    assert delete_calls[0]["issue_id"] == "connection_request_req-7"


@pytest.mark.asyncio
async def test_on_connection_request_mit_preauth_akzeptiert_automatisch():
    """Mit aktiver Pre-Auth → automatisch akzeptieren (REST-POST an accept)."""
    import datetime

    from ha_fleet_agent.remote_access import PreAuthorization
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    session = FakeSession(204)
    mgr = make_manager(session)

    expires = dt_util.utcnow() + datetime.timedelta(hours=2)
    mgr._pre_auth = PreAuthorization(expires_at=expires, max_duration_hours=1)

    data = {
        "request_id": "req-auto",
        "subject": "Auto",
        "reason": "Vorab",
        "duration_hours": 3,  # wird auf max_duration_hours=1 gekürzt
    }
    await mgr._on_connection_request(data)

    accept_call = next(
        (c for c in session.calls if "/connection-requests/req-auto/accept" in c["url"]),
        None,
    )
    assert accept_call is not None
    # Dauer wurde auf max_duration_hours (1) gekappt
    assert accept_call["json"]["duration_hours"] == 1


# --------------------------------------------------------- Tests: Endkunden-Abbruch (§4.4)


@pytest.mark.asyncio
async def test_async_end_session_ohne_session_ist_noop():
    """Ohne laufende Session liefert async_end_session False zurueck."""
    session = FakeSession()
    mgr = make_manager(session)

    result = await mgr.async_end_session()

    assert result is False
    assert mgr.session is None


@pytest.mark.asyncio
async def test_async_end_session_beendet_laufende_session():
    """Mit laufender Session: Session wird genullt, True wird zurueckgegeben."""
    from ha_fleet_agent.remote_access import ActiveSession

    session = FakeSession()
    mgr = make_manager(session)
    mgr._session_obj = ActiveSession(
        request_id="req-1",
        subject="Wartung",
        reason="",
        duration_hours=2,
    )

    result = await mgr.async_end_session(reason="tunnel_closed")

    assert result is True
    assert mgr.session is None
    assert mgr.status == "idle"


# --------------------------------------------------------- Tests: Self-Healing (#90)


@pytest.mark.asyncio
async def test_on_poll_idle_loescht_verwaistes_issue():
    """204 nach offenem Issue → Issue wird entfernt (Integrator-Abbruch/Ablauf)."""
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    # Anfrage geht ein → Repair-Issue offen (open_request_id gesetzt).
    await mgr._on_connection_request(
        {"request_id": "req-x", "subject": "S", "reason": "R", "duration_hours": 2}
    )
    # Poll meldet "nichts offen" — Endkunde hat nicht selbst entschieden.
    await mgr._on_poll_idle()

    delete_calls = [
        c for c in ir._test_calls if c["action"] == "delete"  # type: ignore[attr-defined]
    ]
    assert len(delete_calls) == 1
    assert delete_calls[0]["issue_id"] == "connection_request_req-x"

    # Idempotent: ein zweiter idle-Tick darf nicht erneut loeschen.
    await mgr._on_poll_idle()
    delete_calls = [
        c for c in ir._test_calls if c["action"] == "delete"  # type: ignore[attr-defined]
    ]
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_on_poll_idle_ohne_offenes_issue_ist_noop():
    """Normalfall (alle 15 s): kein offenes Issue → _on_poll_idle loescht nichts."""
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    await mgr._on_poll_idle()

    delete_calls = [
        c for c in ir._test_calls if c["action"] == "delete"  # type: ignore[attr-defined]
    ]
    assert delete_calls == []


@pytest.mark.asyncio
async def test_on_connection_request_andere_id_loescht_altes_issue():
    """FIFO-Wechsel (#90): neue Anfrage-ID → altes verwaistes Issue wird entfernt."""
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    await mgr._on_connection_request(
        {"request_id": "req-alt", "subject": "S", "reason": "R", "duration_hours": 2}
    )
    await mgr._on_connection_request(
        {"request_id": "req-neu", "subject": "S2", "reason": "R2", "duration_hours": 3}
    )

    delete_calls = [
        c for c in ir._test_calls if c["action"] == "delete"  # type: ignore[attr-defined]
    ]
    create_calls = [
        c for c in ir._test_calls if c["action"] == "create"  # type: ignore[attr-defined]
    ]
    assert any(c["issue_id"] == "connection_request_req-alt" for c in delete_calls)
    assert any(c["issue_id"] == "connection_request_req-neu" for c in create_calls)


@pytest.mark.asyncio
async def test_on_connection_request_gleiche_id_kein_flackern():
    """Wiederholter Poll derselben Anfrage (#90): kein delete → kein UI-Flackern.

    Solange eine Anfrage PENDING ist, liefert der Backend-Poll sie alle 15 s
    erneut — das darf das offene Issue nicht abwechselnd loeschen/neu anlegen.
    """
    from homeassistant.helpers import issue_registry as ir

    ir._test_calls.clear()  # type: ignore[attr-defined]
    session = FakeSession()
    mgr = make_manager(session)

    data = {"request_id": "req-same", "subject": "S", "reason": "R", "duration_hours": 2}
    await mgr._on_connection_request(data)
    await mgr._on_connection_request(data)  # gleicher Poll erneut

    delete_calls = [
        c for c in ir._test_calls if c["action"] == "delete"  # type: ignore[attr-defined]
    ]
    assert delete_calls == []


# --------------------------------------------------------- Tests: Wartungs-User-Kopplung (#110)


@pytest.mark.asyncio
async def test_accept_aktiviert_wartungs_user_vor_session():
    """#110: Annahme aktiviert den Wartungs-User; danach laeuft die Session."""
    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)

    await mgr.confirm_request("req-iu", accepted=True, duration_hours=2)

    assert iu.activated == 1, "Wartungs-User muss aktiviert werden"
    assert mgr.session is not None, "Session muss laufen"
    accept_call = next(
        (c for c in session.calls if "/connection-requests/req-iu/accept" in c["url"]),
        None,
    )
    assert accept_call is not None


@pytest.mark.asyncio
async def test_accept_lehnt_ab_wenn_user_nicht_aktivierbar():
    """#110 Fail-Closed: Aktivierung scheitert → reject statt accept, keine Session."""
    session = FakeSession(204)
    iu = FakeIntegratorUser("error")
    mgr = make_manager(session, integrator_user=iu)

    await mgr.confirm_request("req-fail", accepted=True, duration_hours=2)

    assert iu.activated == 1
    assert mgr.session is None, "ohne aktivierbaren User darf keine Session entstehen"
    reject_call = next(
        (c for c in session.calls if "/connection-requests/req-fail/reject" in c["url"]),
        None,
    )
    accept_call = next(
        (c for c in session.calls if "/connection-requests/req-fail/accept" in c["url"]),
        None,
    )
    assert reject_call is not None, "muss reject posten"
    assert accept_call is None, "darf NICHT accept posten"


@pytest.mark.asyncio
async def test_end_session_deaktiviert_wartungs_user():
    """#110: Session-Ende deaktiviert den Wartungs-User."""
    from ha_fleet_agent.remote_access import ActiveSession

    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    mgr._session_obj = ActiveSession(
        request_id="r", subject="s", reason="", duration_hours=2
    )

    result = await mgr.async_end_session(reason="manual")

    assert result is True
    assert iu.deactivated == 1, "Wartungs-User muss deaktiviert werden"
    assert mgr.session is None


@pytest.mark.asyncio
async def test_auto_accept_mit_preauth_aktiviert_user():
    """#110 + §4.3: Auto-Accept per Vorab-Freigabe aktiviert den Wartungs-User ebenfalls."""
    import datetime

    from ha_fleet_agent.remote_access import PreAuthorization
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    mgr._pre_auth = PreAuthorization(
        expires_at=dt_util.utcnow() + datetime.timedelta(hours=2), max_duration_hours=1
    )

    await mgr._on_connection_request(
        {"request_id": "req-pre", "subject": "S", "reason": "R", "duration_hours": 3}
    )

    assert iu.activated == 1
    assert mgr.session is not None


# --------------------------------------------------------- Tests: Session-Resume
# Annahme ohne eigenen Klick (Vorab-Freigabe im Backend, HA-Neustart mitten in der
# Session): Das Plugin erfährt sie nur aus connection_accepted. Ohne Rückfrage
# freigeschaltet wird nur bei laufender/fortsetzbarer Session oder lokal aktiver
# Vorab-Freigabe; sonst bestätigt der Endkunde (#165, Entscheidung 2026-09-23).


def _preauth(mgr, *, hours_valid: float = 8, max_hours: int = 4) -> None:
    import datetime

    from ha_fleet_agent.remote_access import PreAuthorization
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    mgr._pre_auth = PreAuthorization(
        expires_at=dt_util.utcnow() + datetime.timedelta(hours=hours_valid),
        max_duration_hours=max_hours,
    )


def _close_calls(session: FakeSession, request_id: str) -> list[dict]:
    return [
        c for c in session.calls
        if c["url"].endswith(f"/connection-requests/{request_id}/close")
    ]


@pytest.mark.asyncio
async def test_ensure_session_mit_vorab_freigabe_richtet_session_ein_und_aktiviert_user():
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    _preauth(mgr, max_hours=4)
    ends = dt_util.utcnow() + datetime.timedelta(hours=3, minutes=30)

    ok = await mgr.async_ensure_session_for_accepted(
        "req-pre", subject="Heizung", session_expires_at=ends
    )

    assert ok is True
    assert iu.activated == 1
    assert mgr.session is not None
    assert mgr.session.request_id == "req-pre"
    assert mgr.session.subject == "Heizung"
    assert mgr.session.ends_at() == ends, "Früheres Server-Fenster hat Vorrang"
    assert mgr.status == "session_active"


@pytest.mark.asyncio
async def test_ensure_session_kappt_server_fenster_auf_vorab_freigabe_maximum():
    """Ein Server-Fenster länger als die lokale Vorab-Freigabe (Fehler oder
    kompromittiertes Backend) wird auf deren Maximum gekappt."""
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    _preauth(mgr, max_hours=2)
    before = dt_util.utcnow()

    await mgr.async_ensure_session_for_accepted(
        "req-pre", session_expires_at=before + datetime.timedelta(days=3650)
    )

    assert mgr.session is not None
    assert mgr.session.ends_at() <= dt_util.utcnow() + datetime.timedelta(hours=2)
    assert mgr.session.ends_at() >= before + datetime.timedelta(hours=2)


@pytest.mark.asyncio
async def test_ensure_session_ohne_lokale_freigabe_fragt_endkunden():
    """Kein Blindvertrauen ins Backend: ohne laufende Session und ohne lokale
    Vorab-Freigabe kein Freischalten, sondern ein Repair-Issue zur Bestätigung."""
    import datetime

    from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    ir._test_calls.clear()
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)

    ok = await mgr.async_ensure_session_for_accepted(
        "req-x",
        subject="Heizung",
        session_expires_at=dt_util.utcnow() + datetime.timedelta(hours=2),
    )

    assert ok is False
    assert iu.activated == 0, "Wartungs-User darf ohne Zustimmung nicht aktiv werden"
    assert mgr.session is None
    created = [c for c in ir._test_calls if c.get("action") == "create"]
    assert created and created[-1]["issue_id"].endswith("req-x")


@pytest.mark.asyncio
async def test_bestaetigung_nach_rueckfrage_kappt_auf_server_fenster():
    """Bestätigt der Endkunde die Rückfrage mit längerer Dauer, gilt trotzdem das
    Server-Fenster als Ende."""
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    server_end = dt_util.utcnow() + datetime.timedelta(hours=1)
    await mgr.async_ensure_session_for_accepted("req-x", session_expires_at=server_end)

    await mgr.confirm_request("req-x", accepted=True, duration_hours=8)

    # Die Bestätigung startet noch nichts — das Backend hatte schon angenommen, ein
    # Accept-POST (409) unterschiede nicht zwischen ACCEPTED und inzwischen geschlossen.
    assert iu.activated == 0
    assert mgr.session is None
    assert await mgr.async_ensure_session_for_accepted(
        "req-x", session_expires_at=server_end
    ) is True
    assert iu.activated == 1
    assert mgr.session.ends_at() == server_end


@pytest.mark.asyncio
async def test_ablehnung_einer_schon_angenommenen_anfrage_schliesst_sie():
    session = FakeSession(204)
    mgr = make_manager(session, integrator_user=FakeIntegratorUser("ok"))
    await mgr.async_ensure_session_for_accepted("req-x")

    await mgr.confirm_request("req-x", accepted=False)

    assert len(_close_calls(session, "req-x")) == 1
    assert await mgr.async_ensure_session_for_accepted("req-x") is False


@pytest.mark.asyncio
async def test_ensure_session_laufende_session_derselben_anfrage_ist_noop():
    """Manuell angenommene Anfrage: _accept hat Session + User schon eingerichtet —
    connection_accepted darf das Passwort nicht erneut rotieren."""
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    await mgr.confirm_request("req-1", accepted=True, duration_hours=2)
    assert iu.activated == 1

    ok = await mgr.async_ensure_session_for_accepted("req-1")

    assert ok is True
    assert iu.activated == 1, "Kein zweites Aktivieren für dieselbe Anfrage"


@pytest.mark.asyncio
async def test_beendete_session_wird_nie_wiederbelebt():
    """Endkunde trennt → Anfrage gemerkt + im Backend geschlossen. Kommt sie trotzdem
    zurück (DELETE verloren, Close-Notify schneller), kein neuer Zugang — auch nicht
    bei aktiver Vorab-Freigabe; das Schließen wird nachgeholt."""
    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    _preauth(mgr)
    await mgr.async_ensure_session_for_accepted("req-1")
    assert iu.activated == 1

    await mgr.async_end_session(reason="tunnel_closed")

    assert iu.deactivated == 1
    assert len(_close_calls(session, "req-1")) == 1

    ok = await mgr.async_ensure_session_for_accepted("req-1")

    assert ok is False
    assert iu.activated == 1, "Beendete Anfrage darf den User nicht reaktivieren"
    assert len(_close_calls(session, "req-1")) == 2, "Schließen wird nachgeholt"


@pytest.mark.asyncio
async def test_ensure_session_abgelaufenes_server_fenster_verlaengert_nicht():
    """Geht die HA-Uhr vor, liegt ein gültiges Server-Fenster lokal schon hinter uns.
    Fail-closed: kein Tunnel statt einer bis zu 30-tägigen Session."""
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    _preauth(mgr)

    ok = await mgr.async_ensure_session_for_accepted(
        "req-pre", session_expires_at=dt_util.utcnow() - datetime.timedelta(minutes=1)
    )

    assert ok is False
    assert iu.activated == 0
    assert mgr.session is None


@pytest.mark.asyncio
async def test_ensure_session_fail_closed_wenn_user_nicht_aktivierbar():
    """User nicht aktivierbar → keine Session, Anfrage beendet und im Backend
    geschlossen, statt im 15-s-Takt erneut zu scheitern."""
    session = FakeSession(204)
    mgr = make_manager(session, integrator_user=FakeIntegratorUser("error"))
    _preauth(mgr)

    ok = await mgr.async_ensure_session_for_accepted("req-pre")

    assert ok is False
    assert mgr.session is None, "Ohne aktivierbaren Wartungs-User keine Session"
    assert len(_close_calls(session, "req-pre")) == 1
    assert await mgr.async_ensure_session_for_accepted("req-pre") is False


@pytest.mark.asyncio
async def test_ensure_session_ohne_session_ende_nutzt_preauth_dauer():
    """Älteres Backend ohne sessionExpiresAt → Dauer der Vorab-Freigabe."""
    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    _preauth(mgr, max_hours=3)

    await mgr.async_ensure_session_for_accepted("req-pre")

    assert mgr.session is not None
    assert mgr.session.duration_hours == 3


@pytest.mark.asyncio
async def test_session_timer_laeuft_bis_zum_session_ende():
    """Der Ablauf-Timer wird auf die Restzeit bis zum Session-Ende gestellt."""
    import datetime

    from homeassistant.helpers import event  # noqa: PLC0415
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    _preauth(mgr)
    event._test_calls.clear()

    await mgr.async_ensure_session_for_accepted(
        "req-pre", session_expires_at=dt_util.utcnow() + datetime.timedelta(minutes=90)
    )

    delays = [call[1] for call in event._test_calls if call[2] == mgr._on_session_expired]
    assert len(delays) == 1
    assert 90 * 60 - 5 <= delays[0] <= 90 * 60


@pytest.mark.asyncio
async def test_ablauf_einer_fortgesetzten_session_deaktiviert_user():
    """Auch eine aus connection_accepted eingerichtete Session endet mit dem Timer
    fail-closed: User deaktiviert, Anfrage beendet und geschlossen."""
    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    _preauth(mgr)
    await mgr.async_ensure_session_for_accepted("req-pre")

    mgr._on_session_expired(None)
    await mgr._hass.run_tasks()

    assert iu.deactivated == 1
    assert mgr.session is None
    assert len(_close_calls(session, "req-pre")) == 1


@pytest.mark.asyncio
async def test_session_und_vorab_freigabe_ueberstehen_neustart():
    """Neustart: Vorab-Freigabe ist wieder aktiv, die laufende Session wird bei
    ihrem connection_accepted ohne Rückfrage fortgesetzt (User wird wieder aktiv)."""
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    before = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    await before.grant_pre_authorization(expires_in_hours=8, max_duration_hours=4)
    await before.async_ensure_session_for_accepted("req-1", subject="Heizung")
    ends = before.session.ends_at()
    stored = before._store._data

    iu = FakeIntegratorUser("ok")
    after = make_manager(FakeSession(204), integrator_user=iu)
    after._store._data = stored
    await after.async_load()
    after._pre_auth = None  # Fortsetzung darf nicht an der Vorab-Freigabe hängen

    assert after.session is None, "Nach dem Neustart erst nach connection_accepted aktiv"
    ok = await after.async_ensure_session_for_accepted(
        "req-1", session_expires_at=dt_util.utcnow() + datetime.timedelta(hours=8)
    )

    assert ok is True
    assert iu.activated == 1
    assert after.session.subject == "Heizung"
    assert after.session.ends_at() == ends, "Gespeichertes Ende bleibt Obergrenze"


@pytest.mark.asyncio
async def test_vorab_freigabe_wird_nach_neustart_wiederhergestellt():
    before = make_manager(FakeSession(204))
    await before.grant_pre_authorization(expires_in_hours=8, max_duration_hours=3)

    after = make_manager(FakeSession(204))
    after._store._data = before._store._data
    await after.async_load()

    assert after.is_pre_authorized
    assert after.pre_authorization.max_duration_hours == 3


@pytest.mark.asyncio
async def test_beendete_anfragen_ueberstehen_neustart():
    before = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    _preauth(before)
    await before.async_ensure_session_for_accepted("req-1")
    await before.async_end_session(reason="tunnel_closed")

    iu = FakeIntegratorUser("ok")
    after = make_manager(FakeSession(204), integrator_user=iu)
    after._store._data = before._store._data
    await after.async_load()

    assert await after.async_ensure_session_for_accepted("req-1") is False
    assert iu.activated == 0


def test_parse_iso_utc_formate():
    """Backend (Jackson/Instant) liefert ISO-8601 mit Z und bis zu 9 Nachkommastellen."""
    import datetime

    from ha_fleet_agent.remote_access import parse_iso_utc

    utc = datetime.timezone.utc
    assert parse_iso_utc("2026-09-23T12:47:46.966482Z") == datetime.datetime(
        2026, 9, 23, 12, 47, 46, 966482, tzinfo=utc
    )
    nanos = parse_iso_utc("2026-09-23T12:47:46.966482123Z")
    assert nanos is not None and nanos.microsecond == 966482
    assert parse_iso_utc("2026-09-23T14:47:46+02:00") == datetime.datetime(
        2026, 9, 23, 12, 47, 46, tzinfo=utc
    )
    assert parse_iso_utc("2026-09-23T12:47:46").tzinfo == utc, "ohne Zone → UTC"
    assert parse_iso_utc("kein Datum") is None
    assert parse_iso_utc("") is None
    assert parse_iso_utc(None) is None
    assert parse_iso_utc(12345) is None


# --------------------------------------------------------- Tests: Runde 2 (#165)


class RoutedSession(FakeSession):
    """FakeSession mit Status je URL-Endung; ``raise_on`` wirft einen Netzwerkfehler."""

    def __init__(self, routes: dict[str, int] | None = None, raise_on: str | None = None):
        super().__init__(204)
        self._routes = routes or {}
        self._raise_on = raise_on

    def _ctx(self, call: dict):
        self.calls.append(call)
        if self._raise_on and call["url"].endswith(self._raise_on):
            import aiohttp

            raise aiohttp.ClientError("Netz weg")
        for suffix, status in self._routes.items():
            if call["url"].endswith(suffix):
                return _FakeResponse(status)
        return _FakeResponse(self._status)


@pytest.mark.asyncio
async def test_alter_store_ohne_neue_felder_laedt_mit_defaults():
    """Bestandskunden: Der Store aus 1.11.x kennt nur die Konfiguration."""
    mgr = make_manager(FakeSession(204))
    mgr._store._data = {"validity_hours": 6, "max_duration_hours": 3}

    await mgr.async_load()

    assert mgr.validity_hours == 6
    assert mgr.max_duration_hours == 3
    assert mgr.is_pre_authorized is False
    assert mgr._resumable is None
    assert mgr._ended_request_ids == []


@pytest.mark.asyncio
async def test_kaputter_store_wird_verworfen_statt_zu_crashen():
    mgr = make_manager(FakeSession(204))
    mgr._store._data = {
        "validity_hours": 6,
        "max_duration_hours": 3,
        "pre_authorization": {"expires_at": "kein Datum", "max_duration_hours": 4},
        "session": {"request_id": "req-1", "duration_hours": "x"},
        "ended_request_ids": "req-1",
    }

    await mgr.async_load()

    assert mgr.is_pre_authorized is False
    assert mgr._resumable is None
    assert mgr._ended_request_ids == []


@pytest.mark.asyncio
async def test_beendete_anfragen_werden_auf_maximum_gedeckelt():
    from ha_fleet_agent.const import MAX_REMEMBERED_ENDED_REQUESTS

    mgr = make_manager(FakeSession(204))
    for i in range(MAX_REMEMBERED_ENDED_REQUESTS + 5):
        await mgr._mark_ended(f"req-{i}")

    assert len(mgr._ended_request_ids) == MAX_REMEMBERED_ENDED_REQUESTS
    assert "req-0" not in mgr._ended_request_ids
    assert mgr._ended_request_ids[-1] == f"req-{MAX_REMEMBERED_ENDED_REQUESTS + 4}"


@pytest.mark.asyncio
async def test_close_fehler_bleibt_beendet_und_wird_wiederholt():
    """Scheitert das Schließen (5xx oder Netz), bleibt die Anfrage beendet; das
    nächste connection_accepted stößt das Schließen erneut an."""
    for session in (RoutedSession({"/close": 500}), RoutedSession(raise_on="/close")):
        iu = FakeIntegratorUser("ok")
        mgr = make_manager(session, integrator_user=iu)

        await mgr._mark_ended("req-1")
        ok = await mgr.async_ensure_session_for_accepted("req-1")

        assert ok is False
        assert "req-1" in mgr._ended_request_ids
        assert len(_close_calls(session, "req-1")) == 2
        assert iu.activated == 0


@pytest.mark.asyncio
async def test_abgelaufene_fortsetzbare_session_wird_beendet():
    import datetime

    from ha_fleet_agent.remote_access import ActiveSession
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    now = dt_util.utcnow()
    mgr._resumable = ActiveSession(
        request_id="req-1", subject="", reason="", duration_hours=1,
        started_at=now - datetime.timedelta(hours=2),
        expires_at=now - datetime.timedelta(seconds=1),
    )

    ok = await mgr.async_ensure_session_for_accepted("req-1")

    assert ok is False
    assert iu.activated == 0
    assert mgr._resumable is None
    assert "req-1" in mgr._ended_request_ids


@pytest.mark.asyncio
async def test_abgeloeste_fortsetzbare_session_lebt_nicht_wieder_auf():
    """A ist nach dem Neustart fortsetzbar, der Endkunde nimmt B an und beendet B.
    Ein spätes connection_accepted A darf die Wartung nicht fortsetzen."""
    import datetime

    from ha_fleet_agent.remote_access import ActiveSession
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    mgr._resumable = ActiveSession(
        request_id="req-a", subject="", reason="", duration_hours=4,
        expires_at=dt_util.utcnow() + datetime.timedelta(hours=3),
    )

    await mgr.confirm_request("req-b", accepted=True, duration_hours=2)
    await mgr.async_end_session(reason="tunnel_closed")

    assert "req-a" in mgr._ended_request_ids
    assert mgr._store._data["session"] is None
    assert await mgr.async_ensure_session_for_accepted("req-a") is False
    assert iu.activated == 1


@pytest.mark.asyncio
async def test_ablehnen_der_laufenden_anfrage_beendet_session_und_tunnel():
    closed_tunnel: list[int] = []

    async def close_tunnel():
        closed_tunnel.append(1)
        return True

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    mgr.set_session_end_callback(close_tunnel)
    _preauth(mgr)
    await mgr.async_ensure_session_for_accepted("req-1")

    await mgr.confirm_request("req-1", accepted=False)

    assert mgr.session is None
    assert iu.deactivated == 1
    assert closed_tunnel == [1]
    assert "req-1" in mgr._ended_request_ids


@pytest.mark.asyncio
async def test_doppeltes_annehmen_rotiert_nicht_erneut():
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    _preauth(mgr)
    await mgr.async_ensure_session_for_accepted("req-1")
    ends = mgr.session.ends_at()

    await mgr.confirm_request("req-1", accepted=True, duration_hours=8)

    assert iu.activated == 1
    assert mgr.session.ends_at() == ends


@pytest.mark.asyncio
async def test_pending_anfrage_nach_ablehnung_wird_nie_angenommen():
    """Ablehnung ging verloren, Anfrage bleibt PENDING — auch eine spätere
    Vorab-Freigabe nimmt sie nicht an, die Ablehnung wird wiederholt."""
    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    await mgr.confirm_request("req-1", accepted=False)
    _preauth(mgr)

    await mgr._on_connection_request({"requestId": "req-1", "duration": 2})

    assert iu.activated == 0
    assert mgr.session is None
    rejects = [c for c in session.calls if c["url"].endswith("/req-1/reject")]
    assert len(rejects) == 2


@pytest.mark.asyncio
async def test_annehmen_einer_nicht_mehr_offenen_anfrage_startet_keine_session():
    """Repair-Issue noch sichtbar, Anfrage inzwischen abgebrochen: 409 ohne bekannte
    Backend-Annahme → User wieder deaktivieren, keine Session."""
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(RoutedSession({"/req-1/accept": 409}), integrator_user=iu)

    await mgr.confirm_request("req-1", accepted=True, duration_hours=2)

    assert mgr.session is None
    assert iu.deactivated == 1
    assert "req-1" in mgr._ended_request_ids


@pytest.mark.asyncio
async def test_bestaetigte_rueckfrage_startet_erst_mit_connection_accepted():
    """Rückfrage zu einer vom Backend schon angenommenen Anfrage: Das Plugin schickt
    keinen Accept-POST und startet die Session erst, wenn das Backend sie mit dem
    nächsten connection_accepted bestätigt. Hat der Integrator inzwischen geschlossen,
    kommt keins — und es gibt keine Session (#165)."""
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    end = dt_util.utcnow() + datetime.timedelta(hours=1)
    await mgr.async_ensure_session_for_accepted("req-x", session_expires_at=end)

    await mgr.confirm_request("req-x", accepted=True, duration_hours=2)

    assert [c for c in session.calls if c["url"].endswith("/req-x/accept")] == []
    assert mgr.session is None and iu.activated == 0
    assert await mgr.async_ensure_session_for_accepted("req-x", session_expires_at=end)
    assert mgr.session is not None and iu.activated == 1


@pytest.mark.asyncio
async def test_rueckfrage_wird_nach_ablehnung_und_aufraeumen_vergessen():
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    end = dt_util.utcnow() + datetime.timedelta(hours=1)
    await mgr.async_ensure_session_for_accepted("req-x", session_expires_at=end)
    await mgr.async_ensure_session_for_accepted("req-y", session_expires_at=end)
    assert set(mgr._server_expiry) == {"req-x", "req-y"}

    await mgr.confirm_request("req-x", accepted=False)
    await mgr._on_poll_idle()  # Issue von req-y verwaist → aufgeräumt

    assert mgr._server_expiry == {}


@pytest.mark.asyncio
async def test_rueckfrage_ohne_server_ende_schlaegt_konfigurierte_dauer_vor():
    """Älteres Backend ohne sessionExpiresAt: nicht 720 h vorschlagen."""
    from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

    ir._test_calls.clear()
    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    await mgr.set_max_duration_hours(3)

    await mgr.async_ensure_session_for_accepted("req-x")

    created = [c for c in ir._test_calls if c.get("action") == "create"]
    assert created[-1]["data"]["duration_hours"] == 3


@pytest.mark.asyncio
async def test_widerruf_wird_vor_dem_netzwerk_call_persistiert():
    """Hängt der DELETE und HA startet neu, darf die Freigabe nicht zurückkommen."""
    mgr = make_manager(FakeSession(204))
    await mgr.grant_pre_authorization(expires_in_hours=8, max_duration_hours=4)
    stored_at_delete: list = []
    original_delete = mgr._session.delete

    def spying_delete(url, headers=None, timeout=None):
        stored_at_delete.append(mgr._store._data["pre_authorization"])
        return original_delete(url, headers=headers, timeout=timeout)

    mgr._session.delete = spying_delete

    await mgr.revoke_pre_authorization()

    assert stored_at_delete == [None]


@pytest.mark.asyncio
async def test_ablauf_schliesst_den_tunnel():
    closed_tunnel: list[int] = []

    async def close_tunnel():
        closed_tunnel.append(1)
        return True

    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    mgr.set_session_end_callback(close_tunnel)
    _preauth(mgr)
    await mgr.async_ensure_session_for_accepted("req-1")

    mgr._on_session_expired(None)
    await mgr._hass.run_tasks()

    assert closed_tunnel == [1]


# --------------------------------------------------------- Tests: Runde 3 (#165)


class SlowIntegratorUser(FakeIntegratorUser):
    """Aktivierung wartet auf ein Event — öffnet das Await-Fenster in _accept."""

    def __init__(self):
        super().__init__("ok")
        self.release = asyncio.Event()

    async def async_activate(self, *, remove_stale_tokens: bool = True):
        await self.release.wait()
        return await super().async_activate(remove_stale_tokens=remove_stale_tokens)


@pytest.mark.asyncio
async def test_ablehnung_waehrend_accept_verhindert_die_session():
    """Race: Während _accept auf die Aktivierung wartet, lehnt der Endkunde ab
    (zweiter Dialog, Service). Eine beendete Anfrage bekommt nie eine Session."""
    iu = SlowIntegratorUser()
    mgr = make_manager(FakeSession(204), integrator_user=iu)

    accept = asyncio.ensure_future(
        mgr.confirm_request("req-1", accepted=True, duration_hours=2)
    )
    await asyncio.sleep(0)
    await mgr.confirm_request("req-1", accepted=False)
    iu.release.set()
    await accept

    assert mgr.session is None
    assert iu.deactivated >= 1, "Der gerade aktivierte User muss wieder aus"


@pytest.mark.asyncio
async def test_accept_fehler_startet_keine_session():
    """Netzfehler oder 5xx beim Accept: Die Anfrage ist noch PENDING — kein aktiver
    Wartungs-User ohne bestätigtes Fenster; der nächste Poll fragt erneut."""
    for session in (RoutedSession({"/req-1/accept": 503}), RoutedSession(raise_on="/req-1/accept")):
        iu = FakeIntegratorUser("ok")
        mgr = make_manager(session, integrator_user=iu)

        await mgr.confirm_request("req-1", accepted=True, duration_hours=2)

        assert mgr.session is None
        assert iu.deactivated == 1
        assert "req-1" not in mgr._ended_request_ids


@pytest.mark.asyncio
async def test_annehmen_einer_beendeten_anfrage_startet_nichts():
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    await mgr._mark_ended("req-1")

    await mgr.confirm_request("req-1", accepted=True, duration_hours=2)

    assert iu.activated == 0
    assert mgr.session is None


@pytest.mark.asyncio
async def test_fortsetzen_behaelt_browser_tokens_neue_session_nicht():
    """Resume nach HA-Neustart: Die Refresh-Tokens gehören zum noch offenen
    Integrator-Browser dieser Session und bleiben. Eine neue Session räumt sie ab."""
    import datetime

    from ha_fleet_agent.remote_access import ActiveSession
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    iu = FakeIntegratorUser("ok")
    mgr = make_manager(FakeSession(204), integrator_user=iu)
    mgr._resumable = ActiveSession(
        request_id="req-1", subject="", reason="", duration_hours=4,
        expires_at=dt_util.utcnow() + datetime.timedelta(hours=3),
    )
    await mgr.async_ensure_session_for_accepted("req-1")
    await mgr.async_end_session(reason="tunnel_closed")
    _preauth(mgr)
    await mgr.async_ensure_session_for_accepted("req-2")

    assert iu.kept_tokens == [True, False]


@pytest.mark.asyncio
async def test_waehrend_ha_aus_abgelaufene_session_wird_beim_laden_abgeraeumt():
    """Die Session lief ab, während HA aus war — ohne reguläres Ende, also ohne
    Token-Kill. Beim Laden: User deaktivieren (samt Tokens), Anfrage beenden."""
    import datetime

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    past = dt_util.utcnow() - datetime.timedelta(hours=1)
    mgr._store._data = {
        "session": {
            "request_id": "req-1", "subject": "", "reason": "", "duration_hours": 1,
            "started_at": (past - datetime.timedelta(hours=1)).isoformat(),
            "expires_at": past.isoformat(),
        },
    }

    await mgr.async_load()

    assert iu.deactivated == 1
    assert mgr._resumable is None
    assert "req-1" in mgr._ended_request_ids
    assert len(_close_calls(session, "req-1")) == 1


@pytest.mark.asyncio
async def test_unload_beendet_auch_fortsetzbare_session_und_sperrt_neue():
    import datetime

    from ha_fleet_agent.remote_access import ActiveSession
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    session = FakeSession(204)
    iu = FakeIntegratorUser("ok")
    mgr = make_manager(session, integrator_user=iu)
    _preauth(mgr)
    mgr._resumable = ActiveSession(
        request_id="req-a", subject="", reason="", duration_hours=4,
        expires_at=dt_util.utcnow() + datetime.timedelta(hours=3),
    )

    await mgr.async_end_for_unload()

    assert "req-a" in mgr._ended_request_ids
    assert mgr._store._data["session"] is None
    # Ein noch laufender Poll-Handler darf nach dem Unload nichts mehr starten.
    assert await mgr.async_ensure_session_for_accepted("req-b") is False
    assert iu.activated == 0
