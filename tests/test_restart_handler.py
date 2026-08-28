"""Tests fuer den RestartHandler (restart-Poll-Aktion, #127 Sofort-Weg + Rueckmeldung)."""

from __future__ import annotations

import pytest

from ha_fleet_agent.restart_handler import (
    ACK_FAILED,
    ACK_RESTARTING,
    SUPERVISOR_RESTART_URL,
    SUPERVISOR_TOKEN_ENV,
    RestartHandler,
)


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


class FakeConfig:
    """hass.config-Stub — nur `components` wird gebraucht (Supervisor-Erkennung, #144)."""

    def __init__(self, supervisor: bool = True):
        self.components = {"homeassistant"} | ({"hassio"} if supervisor else set())


class FakeHass:
    def __init__(self, fail: bool = False, supervisor: bool = True):
        self.services = FakeServices(fail)
        self.config = FakeConfig(supervisor)


class _FakeResponse:
    def __init__(self, status: int = 204, body: str = ""):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    """aiohttp.ClientSession-Stub — zeichnet POSTs auf (Quittungen UND Supervisor-API).

    ``supervisor_status`` steuert die Antwort der Supervisor-API, ``supervisor_error`` laesst
    den Aufruf werfen (Verbindungsabbruch). Die Quittungen sind davon unberuehrt.
    """

    def __init__(
        self,
        status: int = 204,
        supervisor_status: int = 200,
        supervisor_error: Exception | None = None,
    ):
        self._status = status
        self._supervisor_status = supervisor_status
        self._supervisor_error = supervisor_error
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
        self.posts.append({"url": url, "json": json, "headers": headers})
        if url == SUPERVISOR_RESTART_URL:
            if self._supervisor_error is not None:
                raise self._supervisor_error
            return _FakeResponse(self._supervisor_status)
        return _FakeResponse(self._status)

    @property
    def acks(self) -> list[dict]:
        """Nur die Quittungen ans Backend — ohne den Supervisor-Aufruf."""
        return [p for p in self.posts if p["url"] != SUPERVISOR_RESTART_URL]


def _handler(hass: FakeHass, session: FakeSession) -> RestartHandler:
    return RestartHandler(
        hass, session, "https://api.ha-fleet-manager.com", "secret-key"
    )


# --------------------------------------------------------- Tests


@pytest.mark.asyncio
async def test_restart_ruft_homeassistant_restart_nicht_blockierend():
    """handle ruft homeassistant.restart — bewusst blocking=False (der Prozess endet)."""
    hass = FakeHass()
    await _handler(hass, FakeSession()).handle({"action": "restart"})

    assert len(hass.services.calls) == 1
    call = hass.services.calls[0]
    assert call["domain"] == "homeassistant"
    assert call["service"] == "restart"
    assert call["data"] == {}
    assert call["blocking"] is False


@pytest.mark.asyncio
async def test_quittung_geht_vor_dem_service_call_raus():
    """Nach dem Service-Call ist der Prozess weg — die Quittung muss davor liegen."""
    hass = FakeHass()
    session = FakeSession()
    await _handler(hass, session).handle({"action": "restart"})

    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == "https://api.ha-fleet-manager.com/api/agent/restart-ack"
    assert post["json"] == {"status": ACK_RESTARTING}
    assert post["headers"]["X-API-Key"] == "secret-key"


@pytest.mark.asyncio
async def test_service_fehler_meldet_failed_und_crasht_nicht():
    """Faellt der Service-Aufruf aus, meldet der Handler `failed` — ohne zu werfen."""
    hass = FakeHass(fail=True)
    session = FakeSession()
    await _handler(hass, session).handle({"action": "restart"})  # darf nicht werfen

    assert len(hass.services.calls) == 1  # wurde versucht
    assert [p["json"]["status"] for p in session.posts] == [ACK_RESTARTING, ACK_FAILED]
    assert "homeassistant.restart failed" in session.posts[1]["json"]["error"]


@pytest.mark.asyncio
async def test_fehlgeschlagene_quittung_stoppt_den_neustart_nicht():
    """Die Rueckmeldung ist Beiwerk — ein HTTP-Fehler darf den Neustart nicht verhindern."""
    hass = FakeHass()
    session = FakeSession(status=500)
    await _handler(hass, session).handle({"action": "restart"})

    assert hass.services.calls[0]["service"] == "restart"


@pytest.mark.asyncio
async def test_handle_toleriert_leere_data():
    """Ohne Umfang gilt `core` — ein leeres dict genuegt."""
    hass = FakeHass()
    await _handler(hass, FakeSession()).handle({})

    assert hass.services.calls[0]["service"] == "restart"


@pytest.mark.asyncio
async def test_scope_host_ruft_den_geraete_neustart():
    """scope=host startet das ganze Geraet ueber den Supervisor (#144)."""
    hass = FakeHass()
    await _handler(hass, FakeSession()).handle({"action": "restart", "scope": "host"})

    call = hass.services.calls[0]
    assert call["domain"] == "hassio"
    assert call["service"] == "host_reboot"
    assert call["blocking"] is False


@pytest.mark.asyncio
async def test_scope_host_ohne_supervisor_meldet_failed_ohne_service_call():
    """Ohne Supervisor gibt es kein hassio.host_reboot — sofort mit Ursache scheitern."""
    hass = FakeHass(supervisor=False)
    session = FakeSession()
    await _handler(hass, session).handle({"action": "restart", "scope": "host"})

    assert hass.services.calls == []
    assert [p["json"]["status"] for p in session.posts] == [ACK_FAILED]
    assert "Supervisor" in session.posts[0]["json"]["error"]


@pytest.mark.asyncio
async def test_scope_supervisor_ruft_die_supervisor_api(monkeypatch):
    """scope=supervisor geht ueber die Supervisor-API — einen HA-Service dafuer gibt es nicht."""
    monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "supervisor-token")
    hass = FakeHass()
    session = FakeSession()
    await _handler(hass, session).handle({"action": "restart", "scope": "supervisor"})

    # Kein Service-Call: hassio.supervisor_restart existiert in Home Assistant nicht.
    assert hass.services.calls == []
    call = session.posts[0]
    assert call["url"] == SUPERVISOR_RESTART_URL
    assert call["headers"]["Authorization"] == "Bearer supervisor-token"
    # Quittung erst NACH dem Aufruf — hier gibt es keinen Prozess, der vorher endet.
    assert [p["json"]["status"] for p in session.acks] == [ACK_RESTARTING]


@pytest.mark.asyncio
async def test_scope_supervisor_meldet_http_fehler_statt_scheinerfolg(monkeypatch):
    """Antwortet die Supervisor-API nicht mit 2xx, ist das ein Fehlschlag — kein `restarting`."""
    monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "supervisor-token")
    session = FakeSession(supervisor_status=403)
    await _handler(FakeHass(), session).handle({"action": "restart", "scope": "supervisor"})

    assert [p["json"]["status"] for p in session.acks] == [ACK_FAILED]
    assert "403" in session.acks[0]["json"]["error"]


@pytest.mark.asyncio
async def test_scope_supervisor_meldet_netzwerkfehler(monkeypatch):
    """Bricht der Aufruf ab, geht `failed` raus — der Handler wirft nie."""
    monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "supervisor-token")
    session = FakeSession(supervisor_error=RuntimeError("connection reset"))
    await _handler(FakeHass(), session).handle({"action": "restart", "scope": "supervisor"})

    assert [p["json"]["status"] for p in session.acks] == [ACK_FAILED]
    assert "connection reset" in session.acks[0]["json"]["error"]


@pytest.mark.asyncio
async def test_scope_supervisor_ohne_token_meldet_failed(monkeypatch):
    """Ohne SUPERVISOR_TOKEN gibt es keinen Zugang zur Supervisor-API."""
    monkeypatch.delenv(SUPERVISOR_TOKEN_ENV, raising=False)
    session = FakeSession()
    await _handler(FakeHass(), session).handle({"action": "restart", "scope": "supervisor"})

    assert session.posts and session.posts[0]["url"] != SUPERVISOR_RESTART_URL
    assert [p["json"]["status"] for p in session.acks] == [ACK_FAILED]
    assert SUPERVISOR_TOKEN_ENV in session.acks[0]["json"]["error"]


@pytest.mark.asyncio
async def test_scope_supervisor_ohne_supervisor_meldet_failed(monkeypatch):
    """Ohne Supervisor-Integration wird gar nicht erst aufgerufen."""
    monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "supervisor-token")
    hass = FakeHass(supervisor=False)
    session = FakeSession()
    await _handler(hass, session).handle({"action": "restart", "scope": "supervisor"})

    assert hass.services.calls == []
    assert all(p["url"] != SUPERVISOR_RESTART_URL for p in session.posts)
    assert [p["json"]["status"] for p in session.acks] == [ACK_FAILED]
    assert "Supervisor" in session.acks[0]["json"]["error"]


@pytest.mark.asyncio
async def test_unbekannter_scope_faellt_auf_core_zurueck():
    """Ein unbekannter Umfang darf nie den groesseren Eingriff ausloesen."""
    hass = FakeHass()
    await _handler(hass, FakeSession()).handle({"action": "restart", "scope": "alles"})

    assert hass.services.calls[0]["domain"] == "homeassistant"
