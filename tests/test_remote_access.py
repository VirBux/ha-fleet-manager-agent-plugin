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

    async def async_activate(self):
        self.activated += 1
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
