"""Tests für den HealthMonitor — Systemgesundheit je HA-Bestandteil (#147)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest

from ha_fleet_agent.health import HealthMonitor
from ha_fleet_agent.const import (
    HEALTH_LOOP_LAG_ERROR_MS,
    HEALTH_LOOP_LAG_INTERVAL_SECONDS,
    HEALTH_LOOP_LAG_WARN_MS,
    HEALTH_REASON_ADDON_ERROR,
    HEALTH_REASON_ADDON_STOPPED,
    HEALTH_REASON_ENTITIES_UNAVAILABLE,
    HEALTH_REASON_FRONTEND_BUNDLE_MISSING,
    HEALTH_REASON_FRONTEND_NO_BUNDLE_REF,
    HEALTH_REASON_FRONTEND_UNREACHABLE,
    HEALTH_REASON_INTEGRATION_SETUP_ERROR,
    HEALTH_REASON_INTEGRATION_SETUP_RETRY,
    HEALTH_REASON_LOOP_LAG,
    HEALTH_REASON_RECORDER_DB_LARGE,
    HEALTH_REASON_RECORDER_MISSING,
    HEALTH_REASON_SUPERVISOR_ISSUES,
    HEALTH_REASON_SUPERVISOR_UNHEALTHY,
    HEALTH_REASON_SUPERVISOR_UNREACHABLE,
    HEALTH_REASON_SUPERVISOR_UNSUPPORTED,
    HEALTH_REASON_WS_UNREACHABLE,
    HEALTH_RECORDER_DB_WARN_BYTES,
    HEALTH_STATUS_ERROR,
    HEALTH_STATUS_OK,
    HEALTH_STATUS_WARN,
    HEALTH_WS_PROBE_CHECK_INTERVAL_SECONDS,
)

# Startseiten-HTML, gekürzt auf das, worauf es ankommt — so liefert es die
# Test-VM (HA 2026.6.2) tatsächlich aus.
_REAL_HTML = (
    '<!DOCTYPE html><html><head><title>Home Assistant</title>'
    '<link rel="manifest" href="/manifest.json" crossorigin="use-credentials">'
    '<link rel="modulepreload" href="/frontend_latest/core.44512df9296e30ea.js" '
    'crossorigin="use-credentials">'
    '<link rel="modulepreload" href="/frontend_latest/app.50d85bc030d72d6e.js">'
    "</head><body></body></html>"
)


# --------------------------------------------------------- Stubs


class _FakeResponse:
    def __init__(self, status: int, text: str = "", payload: Any = None):
        self.status = status
        self._text = text
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._payload


class FakeSession:
    """aiohttp-Stub: Antworten pro URL vorgeben, Aufrufe mitschreiben."""

    def __init__(self) -> None:
        self.get_responses: dict[str, Any] = {}
        self.head_responses: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []

    def _resolve(self, table: dict[str, Any], url: str):
        for key, value in table.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return _FakeResponse(404)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        return self._resolve(self.get_responses, url)

    def head(self, url, timeout=None):
        self.calls.append(("HEAD", url))
        return self._resolve(self.head_responses, url)


class FakeHass:
    def __init__(self, states: list[Any] | None = None) -> None:
        self.states = MagicMock()
        self.states.async_all = MagicMock(return_value=states or [])
        self.config_entries = MagicMock()
        self.config_entries.async_entries = MagicMock(return_value=[])
        self.config = MagicMock()
        self.config.path = lambda name: "/config/" + name

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _state(value: str):
    return SimpleNamespace(state=value)


def _monitor(hass: FakeHass | None = None, session: FakeSession | None = None) -> HealthMonitor:
    return HealthMonitor(hass or FakeHass(), session or FakeSession())


# --------------------------------------------------------- Kern (Loop-Lag)


def test_core_ok_unter_der_warnschwelle():
    entry = _monitor()._check_core(HEALTH_LOOP_LAG_WARN_MS - 1)
    assert entry["status"] == HEALTH_STATUS_OK
    assert "reason" not in entry


def test_core_gelb_ab_warnschwelle():
    entry = _monitor()._check_core(HEALTH_LOOP_LAG_WARN_MS)
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_LOOP_LAG


def test_core_rot_ab_fehlerschwelle():
    entry = _monitor()._check_core(HEALTH_LOOP_LAG_ERROR_MS + 500)
    assert entry["status"] == HEALTH_STATUS_ERROR
    # Der konkrete Wert gehört in den Tooltip — "gelb" allein hilft niemandem.
    assert entry["detail"].endswith(" ms")


@pytest.mark.asyncio
async def test_loop_lag_worker_misst_die_verspaetung(monkeypatch):
    """Ein hängender Loop zeigt sich als Differenz zur geplanten Schlafdauer.

    Statt echter Zeit läuft hier eine gestellte Uhr: der Schlaf soll
    ``HEALTH_LOOP_LAG_INTERVAL_SECONDS`` dauern, kommt aber 0,75 s zu spät
    zurück — genau das ist der Lag.
    """
    monitor = _monitor()
    loop = asyncio.get_event_loop()
    clock = {"t": 0.0}
    monkeypatch.setattr(loop, "time", lambda: clock["t"])

    runs = {"n": 0}

    async def _fake_sleep(_seconds):
        runs["n"] += 1
        if runs["n"] > 1:
            raise asyncio.CancelledError
        clock["t"] += HEALTH_LOOP_LAG_INTERVAL_SECONDS + 0.75

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    await monitor._loop_lag_worker()
    assert monitor._loop_lag_max_ms == pytest.approx(750.0, abs=1.0)


# --------------------------------------------------------- Oberfläche


@pytest.mark.asyncio
async def test_frontend_gruen_wenn_bundle_per_head_antwortet():
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(200, _REAL_HTML)
    session.head_responses["core.44512df9296e30ea.js"] = _FakeResponse(200)
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_OK
    # Geprüft wird genau das referenzierte Bundle, nicht ein geratener Pfad.
    assert ("HEAD", "http://127.0.0.1:8123/frontend_latest/core.44512df9296e30ea.js") in session.calls


@pytest.mark.asyncio
async def test_frontend_rot_wenn_das_bundle_fehlt_obwohl_html_kommt():
    """Der beobachtete Fall: HTML mit 200, Bundle tot — ein Statuscode-Check
    auf die Startseite hätte hier "gesund" gemeldet."""
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(200, _REAL_HTML)
    session.head_responses["core.44512df9296e30ea.js"] = _FakeResponse(503)
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_FRONTEND_BUNDLE_MISSING
    assert "503" in entry["detail"]


@pytest.mark.asyncio
async def test_frontend_rot_wenn_html_nicht_erreichbar():
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(500)
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_FRONTEND_UNREACHABLE


@pytest.mark.asyncio
async def test_frontend_gelb_wenn_kein_bundle_referenziert_ist():
    """Ändert HA sein Ausliefer-Schema, ist das kein bewiesener Defekt."""
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(200, "<html><body>nix</body></html>")
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_FRONTEND_NO_BUNDLE_REF


@pytest.mark.asyncio
async def test_frontend_findet_auch_den_alten_es5_ordner():
    """Ältere HA-Versionen lieferten /frontend_es5/ aus — der Pfad wird
    gelesen, nicht konstruiert, also trägt derselbe Code beide."""
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(
        200, '<link rel="modulepreload" href="/frontend_es5/core.abc123.js">'
    )
    session.head_responses["/frontend_es5/core.abc123.js"] = _FakeResponse(200)
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_OK


# --------------------------------------------------------- Datenbank


@pytest.mark.asyncio
async def test_datenbank_rot_ohne_recorder(monkeypatch):
    monitor = _monitor()

    async def _kein_recorder(self):
        return None

    monkeypatch.setattr(HealthMonitor, "_resolve_recorder_db_url", _kein_recorder)
    entry = await monitor._check_database()
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_RECORDER_MISSING


@pytest.mark.asyncio
async def test_datenbank_gruen_bei_externer_db(monkeypatch):
    """MariaDB läuft, ist von hier aus aber nicht messbar — das ist eine
    andere Bauweise, kein fehlender Wert."""
    monitor = _monitor()

    async def _mariadb(self):
        return "mysql://user:pw@10.0.0.5/homeassistant"

    monkeypatch.setattr(HealthMonitor, "_resolve_recorder_db_url", _mariadb)
    entry = await monitor._check_database()
    assert entry["status"] == HEALTH_STATUS_OK
    assert "reason" not in entry


@pytest.mark.asyncio
async def test_datenbank_gelb_bei_grosser_sqlite_datei(monkeypatch):
    monitor = _monitor()

    async def _sqlite(self):
        return "sqlite:////config/home-assistant_v2.db"

    monkeypatch.setattr(HealthMonitor, "_resolve_recorder_db_url", _sqlite)
    monkeypatch.setattr(
        HealthMonitor,
        "_db_size_bytes",
        staticmethod(lambda _p: HEALTH_RECORDER_DB_WARN_BYTES + 1),
    )
    entry = await monitor._check_database()
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_RECORDER_DB_LARGE


def test_sqlite_pfad_aus_db_url():
    # Vier Slashes = absoluter Pfad (der HAOS-Normalfall).
    assert HealthMonitor._sqlite_path("sqlite:////config/home-assistant_v2.db") == (
        "/config/home-assistant_v2.db"
    )
    assert HealthMonitor._sqlite_path("sqlite:///relativ.db") == "/relativ.db"
    # Externe DB: kein Dateipfad, also keine Größenprüfung.
    assert HealthMonitor._sqlite_path("postgresql://host/db") is None


def test_db_groesse_zaehlt_die_wal_datei(tmp_path):
    """Bei aktivem WAL — HAs Standard — liegt ein Teil der Daten in -wal."""
    db = tmp_path / "home-assistant_v2.db"
    db.write_bytes(b"x" * 100)
    (tmp_path / "home-assistant_v2.db-wal").write_bytes(b"y" * 40)
    assert HealthMonitor._db_size_bytes(str(db)) == 140


def test_db_groesse_none_wenn_die_datei_fehlt(tmp_path):
    assert HealthMonitor._db_size_bytes(str(tmp_path / "gibt-es-nicht.db")) is None


# --------------------------------------------------------- Supervisor


def _resolution(**data) -> dict:
    base = {"unhealthy": [], "unsupported": [], "issues": [], "suggestions": []}
    base.update(data)
    return {"result": "ok", "data": base}


@pytest.mark.asyncio
async def test_supervisor_fehlt_ohne_token(monkeypatch):
    """Container-Installation ohne Supervisor: der Bestandteil fehlt komplett,
    statt als grauer Punkt ein Problem zu suggerieren."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert await _monitor()._check_supervisor() is None


@pytest.mark.asyncio
async def test_supervisor_gruen_bei_leeren_listen(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    session = FakeSession()
    session.get_responses["/resolution/info"] = _FakeResponse(200, payload=_resolution())
    entry = await _monitor(session=session)._check_supervisor()
    assert entry["status"] == HEALTH_STATUS_OK


@pytest.mark.asyncio
async def test_supervisor_rot_bei_unhealthy(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    session = FakeSession()
    session.get_responses["/resolution/info"] = _FakeResponse(
        200, payload=_resolution(unhealthy=["docker"])
    )
    entry = await _monitor(session=session)._check_supervisor()
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_SUPERVISOR_UNHEALTHY
    assert entry["detail"] == "docker"


@pytest.mark.asyncio
async def test_supervisor_gelb_bei_unsupported(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    session = FakeSession()
    session.get_responses["/resolution/info"] = _FakeResponse(
        200, payload=_resolution(unsupported=["os_agent"])
    )
    entry = await _monitor(session=session)._check_supervisor()
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_SUPERVISOR_UNSUPPORTED


@pytest.mark.asyncio
async def test_supervisor_gelb_bei_offenen_issues(monkeypatch):
    """Genau der Befund der Test-VM: ein fehlgeschlagener systemd-Unit."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    session = FakeSession()
    session.get_responses["/resolution/info"] = _FakeResponse(
        200,
        payload=_resolution(
            issues=[
                {
                    "type": "systemd_unit_failed",
                    "context": "system",
                    "reference": "hassos-overlay.service",
                    "uuid": "c7ce0bf407e448618c4323b1a1a90b5f",
                }
            ]
        ),
    )
    entry = await _monitor(session=session)._check_supervisor()
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_SUPERVISOR_ISSUES
    # Die UUID taugt für keinen Tooltip, die Referenz schon.
    assert entry["detail"] == "hassos-overlay.service"


@pytest.mark.asyncio
async def test_supervisor_rot_wenn_er_nicht_antwortet(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    session = FakeSession()
    session.get_responses["/resolution/info"] = _FakeResponse(502)
    entry = await _monitor(session=session)._check_supervisor()
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_SUPERVISOR_UNREACHABLE


# --------------------------------------------------------- Add-ons


def test_addons_fehlen_bei_leerer_liste():
    assert HealthMonitor._check_addons([]) is None
    assert HealthMonitor._check_addons(None) is None


def test_addons_gruen_wenn_alle_laufen():
    entry = HealthMonitor._check_addons(
        [{"slug": "core_ssh", "name": "Terminal & SSH", "status": "running"}]
    )
    assert entry["status"] == HEALTH_STATUS_OK


def test_addons_gelb_bei_gestopptem_addon():
    entry = HealthMonitor._check_addons(
        [
            {"slug": "core_ssh", "name": "Terminal & SSH", "status": "running"},
            {"slug": "core_mariadb", "name": "MariaDB", "status": "stopped"},
        ]
    )
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_ADDON_STOPPED
    assert entry["detail"] == "MariaDB"


def test_addons_rot_schlaegt_gelb():
    """Ein abgestürztes Add-on darf nicht von einem gestoppten überdeckt werden."""
    entry = HealthMonitor._check_addons(
        [
            {"slug": "a", "name": "A", "status": "stopped"},
            {"slug": "b", "name": "B", "status": "error"},
        ]
    )
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_ADDON_ERROR
    assert entry["detail"] == "B"


# --------------------------------------------------------- Integrationen


def _entry_stub(domain: str, state, disabled_by=None):
    from homeassistant.config_entries import ConfigEntryState  # noqa: F401

    return SimpleNamespace(domain=domain, state=state, disabled_by=disabled_by)


def test_integrationen_trennen_retry_von_error():
    """Genau der Unterschied, den die bestehende integrations-Liste einebnet:
    setup_retry versucht es noch, setup_error hat aufgegeben."""
    from homeassistant.config_entries import ConfigEntryState

    hass = FakeHass()
    hass.config_entries.async_entries = MagicMock(
        return_value=[_entry_stub("zwave_js", ConfigEntryState.SETUP_RETRY)]
    )
    entry = _monitor(hass)._check_integrations(None)
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_INTEGRATION_SETUP_RETRY
    assert entry["detail"] == "zwave_js"

    hass.config_entries.async_entries = MagicMock(
        return_value=[
            _entry_stub("zwave_js", ConfigEntryState.SETUP_RETRY),
            _entry_stub("hue", ConfigEntryState.SETUP_ERROR),
        ]
    )
    entry = _monitor(hass)._check_integrations(None)
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_INTEGRATION_SETUP_ERROR
    assert entry["detail"] == "hue"


def test_integrationen_ignorieren_bewusst_deaktivierte():
    from homeassistant.config_entries import ConfigEntryState

    hass = FakeHass()
    hass.config_entries.async_entries = MagicMock(
        return_value=[
            _entry_stub("hue", ConfigEntryState.SETUP_ERROR, disabled_by="user")
        ]
    )
    assert _monitor(hass)._check_integrations(None)["status"] == HEALTH_STATUS_OK


def test_unavailable_quote_hebt_auf_gelb():
    hass = FakeHass()
    entry = _monitor(hass)._check_integrations(42.0)
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_ENTITIES_UNAVAILABLE


def test_unavailable_quote_ueberschreibt_keinen_setup_error():
    from homeassistant.config_entries import ConfigEntryState

    hass = FakeHass()
    hass.config_entries.async_entries = MagicMock(
        return_value=[_entry_stub("hue", ConfigEntryState.SETUP_ERROR)]
    )
    entry = _monitor(hass)._check_integrations(99.0)
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_INTEGRATION_SETUP_ERROR


def test_unavailable_quote_rechnet_prozent():
    hass = FakeHass(
        [_state("on"), _state("unavailable"), _state("unknown"), _state("off")]
    )
    assert _monitor(hass)._unavailable_ratio() == 50.0


def test_unavailable_quote_none_ohne_entities():
    assert _monitor(FakeHass([]))._unavailable_ratio() is None


# --------------------------------------------------------- collect()


def test_collect_laesst_ungeprueftes_weg():
    """Bestandteile ohne Befund fehlen im Payload — sie werden nicht auf null
    gesetzt, damit die Anzeige sie weglassen kann statt sie grau zu zeigen."""
    result = _monitor().collect(addons=None)
    assert set(result["components"]) == {"core", "integrations"}
    assert result["loop_lag_ms"] == 0.0


def test_collect_setzt_den_lag_zurueck():
    """Gemeldet wird das Maximum seit dem letzten Push — sonst schleppt ein
    einmaliger Hänger die Ampel für immer mit."""
    monitor = _monitor()
    monitor._loop_lag_max_ms = 1234.5
    assert monitor.collect(None)["loop_lag_ms"] == 1234.5
    assert monitor.collect(None)["loop_lag_ms"] == 0.0


def test_collect_nimmt_die_addons_aus_dem_payload():
    monitor = _monitor()
    result = monitor.collect([{"slug": "core_ssh", "name": "SSH", "status": "running"}])
    assert result["components"]["addons"]["status"] == HEALTH_STATUS_OK


def test_detail_wird_gekappt():
    """Der Payload soll schlank bleiben (#136) — auch bei 50 kaputten Add-ons."""
    addons = [{"slug": f"a{i}", "name": f"Add-on-Nummer-{i}", "status": "stopped"} for i in range(50)]
    entry = HealthMonitor._check_addons(addons)
    assert len(entry["detail"]) <= 120


# --------------------------------------------------------- Auto-Eskalation


class _FakeWs:
    """aiohttp-WebSocket-Stub: eine Nachricht, dann geschlossen."""

    def __init__(self, message: Any = None, error: Exception | None = None):
        self._message = message if message is not None else {"type": "auth_required"}
        self._error = error
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def receive_json(self):
        if self._error is not None:
            raise self._error
        return self._message

    async def close(self):
        self.closed = True


def _with_ws(monitor: HealthMonitor, ws: Any) -> list[str]:
    """Hängt einen ws_connect-Stub an die Session und protokolliert die URLs."""
    urls: list[str] = []

    def _connect(url, timeout=None):
        urls.append(url)
        return ws

    monitor._session.ws_connect = _connect  # type: ignore[attr-defined]
    return urls


@pytest.mark.asyncio
async def test_selbsttest_ok_wenn_ha_auth_required_schickt():
    """Das Frame entsteht im WebSocket-Handler und damit im Event-Loop — genau
    deshalb ist es der bessere Beweis als eine statische Datei."""
    monitor = _monitor()
    urls = _with_ws(monitor, _FakeWs())
    result = await monitor._ws_self_test()
    assert result["result"] == "ok"
    assert urls == ["ws://127.0.0.1:8123/api/websocket"]


@pytest.mark.asyncio
async def test_selbsttest_authentifiziert_sich_nicht():
    """Ein ungültiges auth-Frame liefe in HAs process_wrong_login und könnte
    127.0.0.1 per IP-Ban aussperren — dieselbe Adresse, über die der Tunnel läuft."""
    monitor = _monitor()
    ws = _FakeWs()
    sent: list[Any] = []
    ws.send_json = lambda *a, **k: sent.append(a)  # type: ignore[attr-defined]
    ws.send_str = lambda *a, **k: sent.append(a)  # type: ignore[attr-defined]
    _with_ws(monitor, ws)
    await monitor._ws_self_test()
    assert sent == [], "der Selbsttest darf nichts senden"
    assert ws.closed, "und die Verbindung sauber schließen"


@pytest.mark.asyncio
async def test_selbsttest_meldet_timeout_als_fehlschlag():
    monitor = _monitor()
    _with_ws(monitor, _FakeWs(error=asyncio.TimeoutError()))
    result = await monitor._ws_self_test()
    assert result["result"] == "failed"
    assert result["reason"] == "timeout"


@pytest.mark.asyncio
async def test_selbsttest_meldet_unerwartetes_frame():
    monitor = _monitor()
    _with_ws(monitor, _FakeWs(message={"type": "irgendwas"}))
    result = await monitor._ws_self_test()
    assert result["result"] == "failed"
    assert "irgendwas" in result["reason"]


def test_kein_verdacht_solange_alles_ruhig_ist():
    """Der teure Test darf im Normalbetrieb NIE laufen."""
    monitor = _monitor()
    monitor._loop_lag_max_ms = HEALTH_LOOP_LAG_WARN_MS - 1
    monitor._frontend = {"status": HEALTH_STATUS_OK}
    assert monitor._suspicious() is False


def test_verdacht_bei_hohem_lag_oder_totem_bundle():
    monitor = _monitor()
    monitor._loop_lag_max_ms = HEALTH_LOOP_LAG_WARN_MS
    assert monitor._suspicious() is True

    monitor = _monitor()
    monitor._frontend = {"status": HEALTH_STATUS_ERROR}
    assert monitor._suspicious() is True

    # Ein gelbes Frontend reicht nicht — "lädt langsam" ist kein Ausfall.
    monitor = _monitor()
    monitor._frontend = {"status": HEALTH_STATUS_WARN}
    assert monitor._suspicious() is False


def test_verdacht_setzt_den_lag_nicht_zurueck():
    """Sonst verlöre der Kern-Punkt seine Messgrundlage zwischen zwei Pushes."""
    monitor = _monitor()
    monitor._loop_lag_max_ms = 4000.0
    monitor._suspicious()
    assert monitor._loop_lag_max_ms == 4000.0


def test_fehlgeschlagener_selbsttest_macht_den_kern_rot():
    monitor = _monitor()
    monitor._ws_probe = {"result": "failed", "reason": "timeout", "ms": 10000}
    monitor._ws_probe_at = asyncio.get_event_loop().time()
    entry = monitor._check_core(0.0)
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_WS_UNREACHABLE
    assert entry["detail"] == "timeout"


def test_erfolgreicher_selbsttest_entkraeftet_den_verdacht():
    """Antwortet HA über genau den Kanal, über den die UI lebt, rechtfertigt ein
    hoher Lag-Ausschlag allein kein Rot."""
    monitor = _monitor()
    monitor._ws_probe = {"result": "ok", "ms": 12}
    monitor._ws_probe_at = asyncio.get_event_loop().time()
    entry = monitor._check_core(HEALTH_LOOP_LAG_ERROR_MS + 1000)
    assert entry["status"] == HEALTH_STATUS_WARN
    assert entry["reason"] == HEALTH_REASON_LOOP_LAG


def test_ohne_selbsttest_bleibt_es_beim_lag_urteil():
    monitor = _monitor()
    entry = monitor._check_core(HEALTH_LOOP_LAG_ERROR_MS + 1000)
    assert entry["status"] == HEALTH_STATUS_ERROR
    assert entry["reason"] == HEALTH_REASON_LOOP_LAG


def test_alter_selbsttest_beeinflusst_die_ampel_nicht_mehr():
    """Ein Befund von vor einer Stunde sagt nichts über das Jetzt."""
    monitor = _monitor()
    monitor._ws_probe = {"result": "failed", "reason": "timeout"}
    monitor._ws_probe_at = asyncio.get_event_loop().time() - 3600
    assert monitor._ws_probe_fresh() is None
    assert monitor._check_core(0.0)["status"] == HEALTH_STATUS_OK


def test_collect_traegt_den_selbsttest_als_diagnose_mit():
    monitor = _monitor()
    monitor._ws_probe = {"result": "ok", "ms": 12}
    monitor._ws_probe_at = asyncio.get_event_loop().time()
    result = monitor.collect(None)
    assert result["ws_probe"] == {"result": "ok", "ms": 12}
    # Aber kein eigener Punkt: der Selbsttest ist kein Bestandteil von HA.
    assert "ws_probe" not in result["components"]


def test_collect_ohne_selbsttest_traegt_kein_feld():
    assert "ws_probe" not in _monitor().collect(None)


@pytest.mark.asyncio
async def test_eskalation_drosselt_auf_einen_test_je_fuenf_minuten(monkeypatch):
    """Sonst hämmert eine dauerhaft kranke Instanz sich selbst — und zwar genau
    dann, wenn es ihr ohnehin schlecht geht."""
    monitor = _monitor()
    monitor._loop_lag_max_ms = 99_999.0  # Dauerverdacht

    loop = asyncio.get_event_loop()
    clock = {"t": 0.0}
    monkeypatch.setattr(loop, "time", lambda: clock["t"])

    runs = {"n": 0}
    probes = {"n": 0}

    async def _fake_sleep(_seconds):
        runs["n"] += 1
        if runs["n"] > 4:
            raise asyncio.CancelledError
        # Vier Wächterdurchläufe à 30 s = zwei Minuten Gesamtzeit.
        clock["t"] += HEALTH_WS_PROBE_CHECK_INTERVAL_SECONDS

    async def _fake_probe(self):
        probes["n"] += 1
        return {"result": "ok", "ms": 1}

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(HealthMonitor, "_ws_self_test", _fake_probe)
    await monitor._escalation_worker()

    assert probes["n"] == 1, "in zwei Minuten Dauerverdacht genau ein Test"


@pytest.mark.asyncio
async def test_eskalation_laeuft_nie_wenn_alles_gruen_ist(monkeypatch):
    monitor = _monitor()
    monitor._frontend = {"status": HEALTH_STATUS_OK}

    runs = {"n": 0}
    probes = {"n": 0}

    async def _fake_sleep(_seconds):
        runs["n"] += 1
        if runs["n"] > 10:
            raise asyncio.CancelledError

    async def _fake_probe(self):
        probes["n"] += 1
        return {"result": "ok"}

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(HealthMonitor, "_ws_self_test", _fake_probe)
    await monitor._escalation_worker()

    assert probes["n"] == 0, "solange alles grün ist, kostet die Eskalation nichts"


# --------------------------------------------------------- Lebenszyklus und Robustheit


@pytest.mark.asyncio
async def test_start_startet_alle_drei_tasks_und_stop_bricht_sie_ab():
    monitor = _monitor()
    monitor.start()
    tasks = [monitor._lag_task, monitor._slow_task, monitor._escalation_task]
    assert all(t is not None for t in tasks), "alle drei Worker müssen laufen"

    monitor.stop()
    assert all(t.cancelled() or t.cancelling() or t.done() for t in tasks)
    # Ein zweites stop() darf nicht stolpern (Unload nach fehlgeschlagenem Setup).
    monitor.stop()


@pytest.mark.asyncio
async def test_stop_ohne_start_ist_harmlos():
    _monitor().stop()


@pytest.mark.asyncio
async def test_eine_stolpernde_pruefung_reisst_die_anderen_nicht_mit(monkeypatch):
    """Sonst behielten Datenbank und Supervisor ihren alten Wert — womöglich für
    immer, weil der nächste Durchlauf an derselben Stelle scheitert."""
    monitor = _monitor()

    async def _explodiert(self):
        raise RuntimeError("kaputt")

    async def _db(self):
        return {"status": HEALTH_STATUS_OK}

    async def _sup(self):
        return {"status": HEALTH_STATUS_WARN}

    monkeypatch.setattr(HealthMonitor, "_check_frontend", _explodiert)
    monkeypatch.setattr(HealthMonitor, "_check_database", _db)
    monkeypatch.setattr(HealthMonitor, "_check_supervisor", _sup)

    await monitor._run_slow_checks()

    assert monitor._frontend is None, "die gestolperte Prüfung liefert nichts"
    assert monitor._database == {"status": HEALTH_STATUS_OK}
    assert monitor._supervisor == {"status": HEALTH_STATUS_WARN}


@pytest.mark.asyncio
async def test_stolpernde_pruefung_behaelt_den_letzten_befund(monkeypatch):
    """Dass unsere Prüfung gestolpert ist, sagt nichts über den Bestandteil —
    ihn deshalb aus der Karte zu nehmen wäre gelogen."""
    monitor = _monitor()
    monitor._frontend = {"status": HEALTH_STATUS_ERROR, "reason": "frontend_bundle_missing"}

    async def _explodiert(self):
        raise RuntimeError("kaputt")

    async def _none(self):
        return None

    monkeypatch.setattr(HealthMonitor, "_check_frontend", _explodiert)
    monkeypatch.setattr(HealthMonitor, "_check_database", _none)
    monkeypatch.setattr(HealthMonitor, "_check_supervisor", _none)

    await monitor._run_slow_checks()
    assert monitor._frontend["status"] == HEALTH_STATUS_ERROR


@pytest.mark.asyncio
async def test_selbsttest_meldet_verbindungsfehler():
    """Port zu oder Prozess tot — ein anderer Befund als ein Timeout."""
    monitor = _monitor()

    def _connect(url, timeout=None):
        raise aiohttp.ClientConnectionError("connection refused")

    monitor._session.ws_connect = _connect  # type: ignore[attr-defined]
    result = await monitor._ws_self_test()
    assert result["result"] == "failed"
    assert result["reason"] == "ClientConnectionError"


@pytest.mark.asyncio
async def test_selbsttest_vertraegt_ein_geschlossenes_frame():
    """Schließt die Gegenstelle sofort, wirft aiohttps receive_json einen TypeError."""
    monitor = _monitor()
    _with_ws(monitor, _FakeWs(error=TypeError("Received message 8:None is not str")))
    result = await monitor._ws_self_test()
    assert result["result"] == "failed"
    assert result["reason"] == "TypeError"


@pytest.mark.asyncio
async def test_fehlgeschlagener_selbsttest_haelt_den_verdacht_am_leben():
    """Ohne das bliebe ein Fehlbefund bis zum Ende des Frischefensters stehen,
    obwohl sich die Instanz längst erholt hat — es gäbe keinen Anlass mehr,
    ihn zu widerlegen."""
    monitor = _monitor()
    monitor._loop_lag_max_ms = 0.0
    monitor._frontend = {"status": HEALTH_STATUS_OK}
    monitor._ws_probe = {"result": "failed", "reason": "timeout"}
    assert monitor._suspicious() is True

    monitor._ws_probe = {"result": "ok", "ms": 5}
    assert monitor._suspicious() is False


@pytest.mark.asyncio
async def test_frontend_bevorzugt_das_core_bundle():
    """Ohne core.*.js startet die Oberfläche gar nicht — das ist der aussagekräftigste
    Prüfling, auch wenn im HTML andere Bundles davor stehen."""
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(
        200,
        '<link rel="modulepreload" href="/frontend_latest/app.aaa.js">'
        '<link rel="modulepreload" href="/frontend_latest/core.bbb.js">',
    )
    session.head_responses["core.bbb.js"] = _FakeResponse(200)
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_OK
    assert ("HEAD", "http://127.0.0.1:8123/frontend_latest/core.bbb.js") in session.calls


@pytest.mark.asyncio
async def test_frontend_nimmt_auch_hashes_mit_grossbuchstaben():
    """Ein künftiger Base62-Hash darf nicht stumm als "kein Bundle" durchfallen."""
    session = FakeSession()
    session.get_responses["8123/"] = _FakeResponse(
        200, '<link rel="modulepreload" href="/frontend_latest/core.aB3XyZ.js">'
    )
    session.head_responses["core.aB3XyZ.js"] = _FakeResponse(200)
    entry = await _monitor(session=session)._check_frontend()
    assert entry["status"] == HEALTH_STATUS_OK


def test_collect_haelt_die_anzeige_reihenfolge_ein():
    monitor = _monitor()
    monitor._supervisor = {"status": HEALTH_STATUS_OK}
    monitor._frontend = {"status": HEALTH_STATUS_OK}
    monitor._database = {"status": HEALTH_STATUS_OK}
    result = monitor.collect([{"slug": "a", "name": "A", "status": "running"}])
    assert list(result["components"]) == [
        "core",
        "frontend",
        "database",
        "supervisor",
        "addons",
        "integrations",
    ]
