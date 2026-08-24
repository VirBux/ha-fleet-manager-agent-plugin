"""Tests fuer den RestartHandler (restart-Poll-Aktion, #127 Sofort-Weg + Rueckmeldung)."""

from __future__ import annotations

import pytest

from ha_fleet_agent.restart_handler import ACK_FAILED, ACK_RESTARTING, RestartHandler


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


class _FakeResponse:
    def __init__(self, status: int = 204):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    """aiohttp.ClientSession-Stub — zeichnet die Quittungs-POSTs auf."""

    def __init__(self, status: int = 204):
        self._status = status
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._status)


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
    """Die Aktion traegt keine Nutzdaten — ein leeres dict genuegt."""
    hass = FakeHass()
    await _handler(hass, FakeSession()).handle({})

    assert hass.services.calls[0]["service"] == "restart"
