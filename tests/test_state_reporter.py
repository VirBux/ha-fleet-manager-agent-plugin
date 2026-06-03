"""Tests fuer den StateReporter (REST-Transport, TODO #50.19)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ha_fleet_agent.state_reporter import StateReporter


# --------------------------------------------------------- Stubs


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    """aiohttp.ClientSession-Stub — zeichnet POST-Calls auf."""

    def __init__(self, response_status: int = 200):
        self._response_status = response_status
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._response_status)

    async def close(self):
        pass


class FakeHass:
    """Minimaler hass-Stub fuer StateReporter-Tests."""

    def __init__(self):
        self._tasks = []
        self.config = MagicMock()
        self.config.version = "2026.1.0"
        self.states = MagicMock()
        self.states.async_all = MagicMock(return_value=[])
        self.config_entries = MagicMock()
        self.config_entries.async_entries = MagicMock(return_value=[])
        self.data = {}

    def async_create_task(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task


# --------------------------------------------------------- Tests


@pytest.fixture(autouse=True)
def _disable_external_stats(monkeypatch):
    """Schaltet alle externen Stats-Quellen im Test-Setup aus.

    - `_fetch_core_stats_via_supervisor_api`: kein echter HTTP-Call an Supervisor.
    - `_fetch_host_stats_via_psutil`: kein echter Read auf /proc/* der Test-Maschine.

    Tests, die eine konkrete Quelle prüfen wollen, patchen die jeweilige
    Methode pro Test erneut.
    """
    async def _none(self):
        return None

    from ha_fleet_agent.state_reporter import StateReporter as _SR
    monkeypatch.setattr(_SR, "_fetch_core_stats_via_supervisor_api", _none)
    monkeypatch.setattr(_SR, "_fetch_host_stats_via_psutil", _none)
    yield


@pytest.mark.asyncio
async def test_push_once_sendet_korrekte_url_und_header():
    """_push_once muss POST an /api/agent/state mit X-API-Key senden."""
    session = FakeSession(response_status=200)
    hass = FakeHass()

    reporter = StateReporter(
        hass,
        entry_id="test-entry",
        session=session,
        backend_url="https://api.ha-fleet-manager.com",
        api_key="my-secret-api-key",
    )

    # Supervisor-Info ueberspringen (kein HAOS)
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.ha-fleet-manager.com/api/agent/state"
    assert call["headers"]["X-API-Key"] == "my-secret-api-key"
    assert call["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_payload_enthaelt_kein_api_key_und_kein_type_feld():
    """Der Payload darf api_key und type nicht mehr enthalten (REST-Transport)."""
    session = FakeSession(response_status=200)
    hass = FakeHass()

    reporter = StateReporter(
        hass,
        entry_id="test-entry",
        session=session,
        backend_url="https://api.ha-fleet-manager.com",
        api_key="my-secret-api-key",
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert "api_key" not in payload
    assert "type" not in payload
    assert "timestamp" in payload
    assert "ha_version" in payload


@pytest.mark.asyncio
async def test_payload_enthaelt_agent_version():
    """Der Payload muss die eigene Plugin-Version als agent_version mitsenden (#100).

    Das Backend speichert den Wert pro Installation; das Dashboard zeigt ihn in der
    System-Karte neben der HA-Version an.
    """
    from ha_fleet_agent.const import VERSION

    session = FakeSession(response_status=200)
    hass = FakeHass()

    reporter = StateReporter(
        hass,
        entry_id="test-entry",
        session=session,
        backend_url="https://api.ha-fleet-manager.com",
        api_key="my-secret-api-key",
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["agent_version"] == VERSION


@pytest.mark.asyncio
async def test_5xx_fehler_kein_crash():
    """5xx-Antwort darf keinen Exception werfen — Ticker laeuft weiter."""
    session = FakeSession(response_status=503)
    hass = FakeHass()

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        # Darf keinen Exception werfen
        result = await reporter._post({"timestamp": "2026-01-01T00:00:00Z"})

    assert result is False


@pytest.mark.asyncio
async def test_401_fehler_kein_crash():
    """401/403 soll nur geloggt werden, kein Crash."""
    session = FakeSession(response_status=401)
    hass = FakeHass()

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "bad-key"
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        result = await reporter._post({"timestamp": "2026-01-01T00:00:00Z"})

    assert result is False


@pytest.mark.asyncio
async def test_timeout_kein_crash():
    """aiohttp.TimeoutError soll abgefangen werden."""
    import aiohttp

    class _TimeoutSession:
        def post(self, *a, **kw):
            class _Ctx:
                async def __aenter__(self):
                    raise asyncio.TimeoutError()

                async def __aexit__(self, *_):
                    return False
            return _Ctx()

    hass = FakeHass()
    reporter = StateReporter(
        hass, "e1", _TimeoutSession(), "https://api.ha-fleet-manager.com", "key"
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        result = await reporter._post({"timestamp": "2026-01-01T00:00:00Z"})

    assert result is False


@pytest.mark.asyncio
async def test_2xx_gibt_true_zurueck():
    """HTTP 200 soll True zurueckgeben."""
    session = FakeSession(response_status=200)
    hass = FakeHass()

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        result = await reporter._post({"timestamp": "2026-01-01T00:00:00Z"})

    assert result is True


@pytest.mark.asyncio
async def test_backend_url_mit_trailing_slash():
    """Trailing-Slash in backend_url darf den Endpoint nicht verdoppeln."""
    session = FakeSession(response_status=200)
    hass = FakeHass()

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com/", "key"
    )

    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    url = session.calls[0]["url"]
    assert url == "https://api.ha-fleet-manager.com/api/agent/state"
    assert "//" not in url.replace("https://", "")


@pytest.mark.asyncio
async def test_telemetrie_felder_aus_haos_supervisor_korrekt_gemappt():
    """Regressions-Test (Bug 2026-05-24): cpu_percent/ram_percent kommen aus
    /core/stats (core_stats), disk_percent aus /host/info, ha_version als String.

    Vor dem Fix wurde cpu_percent aus host_info gelesen (existiert dort nicht) →
    NULL in der DB. Dieser Test sichert die richtige Quellen-Zuordnung.
    """
    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"  # simulierter HA-Build

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )

    fake_host_info = {
        "hostname": "homeassistant",
        "operating_system": "Home Assistant OS 13",
        "disk_total": 100_000,  # MB
        "disk_used": 25_000,
    }
    # Add-on-Mock im realen Supervisor-Format (#102): slug/name/version/
    # version_latest/update_available/state — inkl. eines GESTOPPTEN Add-ons,
    # das frueher herausgefiltert wurde.
    fake_supervisor_info = {
        "addons": [
            {
                "slug": "core_mosquitto",
                "name": "Mosquitto broker",
                "version": "6.5.0",
                "version_latest": "6.5.0",
                "update_available": False,
                "state": "started",
            },
            {
                "slug": "a0d7b954_zigbee2mqtt",
                "name": "Zigbee2MQTT",
                "version": "2.5.1",
                "version_latest": "2.6.0",
                "update_available": True,
                "state": "started",
            },
            {
                "slug": "core_configurator",
                "name": "File editor",
                "version": "5.9.0",
                "version_latest": "5.9.0",
                "update_available": False,
                "state": "stopped",
            },
        ],
    }
    fake_core_stats = {
        "cpu_percent": 12.3,
        "memory_percent": 47.8,
        "memory_usage": 512_000_000,
        "memory_limit": 1_073_741_824,
    }

    with patch.object(
        reporter,
        "_fetch_supervisor_info",
        return_value=(fake_host_info, fake_supervisor_info, fake_core_stats),
    ):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["ha_version"] == "2026.5.0", "ha_version muss als String drin sein"
    assert payload["cpu_percent"] == 12.3, "cpu_percent kommt aus core_stats"
    assert payload["ram_percent"] == 47.8, "ram_percent = core_stats.memory_percent"
    assert payload["disk_percent"] == 25.0, "disk_percent = 25000/100000 * 100"
    # Add-ons als strukturierte Objekte (#102) — gestoppte erscheinen jetzt mit.
    addons = payload["addons"]
    assert len(addons) == 3, "alle installierten Add-ons (auch gestoppte) erscheinen"
    assert addons[0] == {
        "slug": "core_mosquitto",
        "name": "Mosquitto broker",
        "status": "running",
        "version": "6.5.0",
        "version_latest": "6.5.0",
        "update_available": False,
    }
    assert addons[1]["update_available"] is True, "Update-Flag wird durchgereicht"
    assert addons[2]["status"] == "stopped", "gestopptes Add-on nicht mehr gefiltert"


def test_list_addons_mappt_status_und_felder():
    """_list_addons normalisiert state→status und reicht alle Felder durch (#102)."""
    supervisor_info = {
        "addons": [
            {
                "slug": "core_ssh",
                "name": "Terminal & SSH",
                "version": "10.2.0",
                "version_latest": "10.3.0",
                "update_available": True,
                "state": "started",
            },
        ]
    }
    assert StateReporter._list_addons(supervisor_info) == [
        {
            "slug": "core_ssh",
            "name": "Terminal & SSH",
            "status": "running",
            "version": "10.2.0",
            "version_latest": "10.3.0",
            "update_available": True,
        }
    ]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("started", "running"),
        ("stopped", "stopped"),
        ("error", "error"),
        ("unknown", "stopped"),
        ("startup", "stopped"),
        (None, "stopped"),
    ],
)
def test_list_addons_status_mapping(state, expected):
    """Supervisor-state wird auf running/stopped/error normalisiert."""
    supervisor_info = {"addons": [{"slug": "x", "name": "X", "state": state}]}
    assert StateReporter._list_addons(supervisor_info)[0]["status"] == expected


def test_list_addons_slug_ungleich_name_bleibt_erhalten():
    """slug (stabiler Key) und name (Anzeige) sind getrennte Felder (#102)."""
    supervisor_info = {
        "addons": [
            {"slug": "a0d7b954_vscode", "name": "Studio Code Server", "state": "started"}
        ]
    }
    entry = StateReporter._list_addons(supervisor_info)[0]
    assert entry["slug"] == "a0d7b954_vscode"
    assert entry["name"] == "Studio Code Server"


def test_list_addons_fehlende_felder_defensiv():
    """Fehlende version/version_latest → None, fehlendes update_available → False."""
    supervisor_info = {"addons": [{"slug": "core_x", "name": "X", "state": "started"}]}
    entry = StateReporter._list_addons(supervisor_info)[0]
    assert entry["version"] is None
    assert entry["version_latest"] is None
    assert entry["update_available"] is False


def test_list_addons_ueberspringt_kaputte_eintraege():
    """Kein-dict-Eintrag und Eintrag ganz ohne Identitaet werden uebersprungen."""
    supervisor_info = {
        "addons": [
            "nicht-ein-dict",
            {"state": "started"},  # weder slug noch name
            {"slug": "core_ok", "name": "OK", "state": "started"},
        ]
    }
    result = StateReporter._list_addons(supervisor_info)
    assert len(result) == 1
    assert result[0]["slug"] == "core_ok"


def test_list_addons_leer_ohne_supervisor():
    """Ohne Supervisor-Info (Nicht-HAOS) bleibt die Liste leer."""
    assert StateReporter._list_addons(None) == []
    assert StateReporter._list_addons({}) == []


# --------------------------------------------------------- updates[] (#103)


class _FakeState:
    """Minimaler hass.states-State-Stub: entity_id + state + attributes."""

    def __init__(self, entity_id: str, state: str, attributes: dict | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}


def _updates_reporter(update_states: list[_FakeState]) -> StateReporter:
    """Reporter, dessen hass.states.async_all('update') die gegebenen States liefert."""
    hass = FakeHass()
    hass.states.async_all = MagicMock(return_value=update_states)
    return StateReporter(
        hass, "e1", FakeSession(), "https://api.ha-fleet-manager.com", "key"
    )


def test_list_updates_addon_extrahiert_slug_aus_entity_picture():
    """Add-on-update: kind=addon, slug aus /api/hassio/addons/<slug>/icon (Research §5)."""
    state = _FakeState(
        "update.terminal_ssh_update",
        "on",
        {
            "title": "Terminal & SSH",
            "installed_version": "10.2.0",
            "latest_version": "10.3.0",
            "entity_picture": "/api/hassio/addons/core_ssh/icon",
            "supported_features": 29,
        },
    )
    entry = _updates_reporter([state])._list_updates()[0]
    assert entry["kind"] == "addon"
    assert entry["slug"] == "core_ssh"
    assert entry["title"] == "Terminal & SSH"
    assert entry["installed_version"] == "10.2.0"
    assert entry["latest_version"] == "10.3.0"
    assert entry["update_available"] is True
    assert entry["supported_features"] == 29


def test_list_updates_core_kind_und_keine_slug():
    """HA-Core-update: kind=core ueber feste Entity-ID, slug=None, feat durchgereicht."""
    state = _FakeState(
        "update.home_assistant_core_update",
        "on",
        {
            "title": "Home Assistant Core",
            "installed_version": "2026.5.1",
            "latest_version": "2026.5.4",
            "supported_features": 15,
            "release_url": "https://www.home-assistant.io/latest-release-notes/",
        },
    )
    entry = _updates_reporter([state])._list_updates()[0]
    assert entry["kind"] == "core"
    assert entry["slug"] is None
    assert entry["supported_features"] == 15
    assert entry["release_url"].startswith("https://")


@pytest.mark.parametrize(
    ("entity_id", "expected_kind"),
    [
        ("update.home_assistant_core_update", "core"),
        ("update.home_assistant_operating_system_update", "os"),
        ("update.home_assistant_supervisor_update", "supervisor"),
    ],
)
def test_list_updates_system_entities_kind(entity_id, expected_kind):
    """Core/OS/Supervisor werden ueber ihre festen Entity-IDs klassifiziert."""
    state = _FakeState(entity_id, "off", {"supported_features": 11})
    entry = _updates_reporter([state])._list_updates()[0]
    assert entry["kind"] == expected_kind
    assert entry["update_available"] is False


def test_list_updates_integration_ist_default():
    """Eine update-Entity ohne System-ID und ohne Add-on-Bild gilt als Integration."""
    state = _FakeState(
        "update.frigate_update",
        "on",
        {"title": "Frigate", "installed_version": "0.13.0", "latest_version": "0.14.0"},
    )
    entry = _updates_reporter([state])._list_updates()[0]
    assert entry["kind"] == "integration"
    assert entry["slug"] is None


def test_list_updates_fehlende_attribute_defensiv():
    """Leere Attribute: title faellt auf entity_id, Versionen None, feat=0, in_progress False."""
    state = _FakeState("update.something", "off", {})
    entry = _updates_reporter([state])._list_updates()[0]
    assert entry["title"] == "update.something"
    assert entry["installed_version"] is None
    assert entry["latest_version"] is None
    assert entry["supported_features"] == 0
    assert entry["in_progress"] is False
    assert entry["update_available"] is False


def test_list_updates_in_progress_durchgereicht():
    """in_progress-Attribut wird als bool durchgereicht."""
    state = _FakeState("update.x", "on", {"in_progress": True, "supported_features": 4})
    entry = _updates_reporter([state])._list_updates()[0]
    assert entry["in_progress"] is True


def test_list_updates_ueberspringt_kaputte_states():
    """Ein State, dessen Zugriff scheitert, reisst die Liste nicht mit."""

    class _BrokenState:
        entity_id = "update.broken"

        @property
        def state(self):  # noqa: ANN201
            raise RuntimeError("kaputt")

        attributes: dict = {}

    good = _FakeState("update.ok", "on", {"supported_features": 1})
    result = _updates_reporter([_BrokenState(), good])._list_updates()
    assert len(result) == 1
    assert result[0]["entity_id"] == "update.ok"


@pytest.mark.parametrize(
    ("entity_id", "picture", "expected"),
    [
        ("update.home_assistant_core_update", None, ("core", None)),
        ("update.home_assistant_operating_system_update", None, ("os", None)),
        ("update.home_assistant_supervisor_update", None, ("supervisor", None)),
        ("update.terminal_ssh_update", "/api/hassio/addons/core_ssh/icon", ("addon", "core_ssh")),
        ("update.vscode_update", "/api/hassio/addons/a0d7b954_vscode/icon?token=x", ("addon", "a0d7b954_vscode")),
        ("update.frigate_update", "/api/frigate/notifications/thumb.jpg", ("integration", None)),
        ("update.hacs_update", None, ("integration", None)),
    ],
)
def test_classify_update(entity_id, picture, expected):
    """(kind, slug)-Ableitung: feste IDs, Add-on-Bild, sonst Integration."""
    assert StateReporter._classify_update(entity_id, picture) == expected


@pytest.mark.asyncio
async def test_payload_enthaelt_updates_liste():
    """End-to-end: _push_once legt das updates[]-Feld in den Payload (#103)."""
    def _async_all(domain=None):
        if domain == "update":
            return [
                _FakeState(
                    "update.terminal_ssh_update",
                    "on",
                    {
                        "title": "Terminal & SSH",
                        "entity_picture": "/api/hassio/addons/core_ssh/icon",
                        "installed_version": "10.2.0",
                        "latest_version": "10.3.0",
                        "supported_features": 29,
                    },
                )
            ]
        return []

    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"
    hass.states.async_all = MagicMock(side_effect=_async_all)

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    updates = session.calls[0]["json"]["updates"]
    assert len(updates) == 1
    assert updates[0]["kind"] == "addon"
    assert updates[0]["slug"] == "core_ssh"


@pytest.mark.asyncio
async def test_ha_version_awesome_version_objekt_wird_zu_string():
    """Wenn hass.config.version ein Objekt ist (AwesomeVersion), muss
    das Plugin str() casten, sonst landet null in der DB."""

    class _AwesomeVersionStub:
        def __str__(self) -> str:
            return "2026.5.0"

    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = _AwesomeVersionStub()

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["ha_version"] == "2026.5.0"


@pytest.mark.asyncio
async def test_cpu_ram_fallback_via_supervisor_api(monkeypatch):
    """Wenn hassio.get_core_stats() leer ist (Subscription-Problem), greift der
    direkte Supervisor-API-Call. CPU + RAM müssen aus der API kommen."""

    async def _fake_direct(self):
        return {"cpu_percent": 7.5, "memory_percent": 33.3, "memory_usage": 100}

    from ha_fleet_agent.state_reporter import StateReporter as _SR
    monkeypatch.setattr(_SR, "_fetch_core_stats_via_supervisor_api", _fake_direct)

    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    # _fetch_supervisor_info liefert core_stats={} → Fallback greift
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, {})):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["cpu_percent"] == 7.5
    assert payload["ram_percent"] == 33.3


@pytest.mark.asyncio
async def test_psutil_host_stats_bevorzugt_vor_container_stats(monkeypatch):
    """Bugfix 2026-05-24 (0.2.3): psutil (Host-Werte) muss /core/stats schlagen.

    Hintergrund: Der User sieht in der HA-UI Host-RAM (z.B. 18.8 %), nicht
    Container-RAM (7.5 %). Wenn psutil verfügbar ist, MUSS dessen Wert gewinnen
    — selbst wenn /core/stats parallel einen Container-Wert liefert.
    """

    async def _fake_psutil(self):
        return {"cpu_percent": 12.0, "memory_percent": 18.8}

    from ha_fleet_agent.state_reporter import StateReporter as _SR
    monkeypatch.setattr(_SR, "_fetch_host_stats_via_psutil", _fake_psutil)

    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )

    # core_stats würde 7.5 % RAM melden — Container-Wert. Soll IGNORIERT werden.
    fake_core_stats = {"cpu_percent": 0.5, "memory_percent": 7.5}
    with patch.object(
        reporter,
        "_fetch_supervisor_info",
        return_value=(None, None, fake_core_stats),
    ):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["cpu_percent"] == 12.0, "psutil-CPU muss core_stats schlagen"
    assert payload["ram_percent"] == 18.8, "psutil-RAM muss core_stats schlagen"


@pytest.mark.asyncio
async def test_list_integrations_meldet_status_und_aggregiert_pro_domain():
    """_list_integrations liefert {domain, status, version} statt nur Namen.

    Prueft die Status-Normalisierung (active/stopped/error), dass die eigene
    Agent-Domain ausgeklammert wird, dass disabled_by Vorrang vor dem state hat
    und dass mehrere Entries derselben Domain mit dem schlechtesten Status
    zusammengefasst werden (worst-case error > stopped > active). Die Version
    (eigener Test unten) ist hier ohne loader-Patch immer None.
    """
    from homeassistant.config_entries import ConfigEntryState

    from ha_fleet_agent.const import DOMAIN

    class _Entry:
        def __init__(self, domain, state, disabled_by=None):
            self.domain = domain
            self.state = state
            self.disabled_by = disabled_by

    hass = FakeHass()
    hass.config_entries.async_entries = MagicMock(
        return_value=[
            _Entry("hue", ConfigEntryState.LOADED),  # active
            _Entry("mqtt", ConfigEntryState.SETUP_RETRY),  # error
            _Entry("knx", ConfigEntryState.NOT_LOADED),  # stopped
            # disabled_by gewinnt, selbst wenn der state (theoretisch) LOADED waere
            _Entry("spotify", ConfigEntryState.LOADED, disabled_by="user"),
            # zwei Instanzen derselben Domain: eine laeuft, eine im Fehler -> error
            _Entry("zha", ConfigEntryState.LOADED),
            _Entry("zha", ConfigEntryState.SETUP_ERROR),
            # eigene Domain muss rausfallen
            _Entry(DOMAIN, ConfigEntryState.LOADED),
        ]
    )

    reporter = StateReporter(
        hass, "e1", FakeSession(), "https://api.ha-fleet-manager.com", "key"
    )
    result = await reporter._list_integrations()

    by_domain = {e["domain"]: e["status"] for e in result}
    assert DOMAIN not in by_domain, "eigene Agent-Domain darf nicht gemeldet werden"
    assert by_domain == {
        "hue": "active",
        "mqtt": "error",
        "knx": "stopped",
        "spotify": "stopped",
        "zha": "error",
    }
    # pro Domain genau ein Eintrag, nach Domain sortiert
    assert [e["domain"] for e in result] == sorted(by_domain)
    # version-Feld ist immer vorhanden (hier None — kein loader-Patch)
    assert all("version" in e for e in result)
    assert all(e["version"] is None for e in result)


@pytest.mark.asyncio
async def test_list_integrations_reichert_manifest_version_an():
    """_list_integrations haengt pro Domain die Manifest-Version an.

    Custom-/HACS-Integrationen fuehren eine Version im Manifest; HA-Core-
    Integrationen meist nicht (-> None). `async_get_integrations` liefert je
    Domain eine Integration (mit `.version`) oder eine Exception (-> None);
    der Status bleibt von der Version unberuehrt.
    """
    from homeassistant.config_entries import ConfigEntryState

    class _Entry:
        def __init__(self, domain, state, disabled_by=None):
            self.domain = domain
            self.state = state
            self.disabled_by = disabled_by

    class _Integration:
        def __init__(self, version):
            self.version = version

    hass = FakeHass()
    hass.config_entries.async_entries = MagicMock(
        return_value=[
            _Entry("frigate", ConfigEntryState.LOADED),  # Custom -> Version
            _Entry("hue", ConfigEntryState.LOADED),  # Core -> keine Version
            _Entry("broken", ConfigEntryState.LOADED),  # Lookup wirft -> None
        ]
    )

    async def _fake_get_integrations(_hass, _domains):
        return {
            "frigate": _Integration("5.6.0"),
            "hue": _Integration(None),
            "broken": RuntimeError("integration not found"),
        }

    reporter = StateReporter(
        hass, "e1", FakeSession(), "https://api.ha-fleet-manager.com", "key"
    )
    with patch(
        "homeassistant.loader.async_get_integrations", _fake_get_integrations
    ):
        result = await reporter._list_integrations()

    by_version = {e["domain"]: e["version"] for e in result}
    assert by_version == {"frigate": "5.6.0", "hue": None, "broken": None}
    # Status bleibt unabhaengig von der Version
    assert all(e["status"] == "active" for e in result)


@pytest.mark.asyncio
async def test_psutil_nicht_verfuegbar_fallback_auf_core_stats(monkeypatch):
    """Wenn psutil None liefert (z.B. keine /proc-Sicht), fällt der Reporter
    auf die Container-Stats aus /core/stats zurück — kein None in der DB.
    """
    # Default-Fixture lässt psutil None liefern — perfekt für diesen Test.
    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    fake_core_stats = {"cpu_percent": 0.5, "memory_percent": 7.5}
    with patch.object(
        reporter,
        "_fetch_supervisor_info",
        return_value=(None, None, fake_core_stats),
    ):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["cpu_percent"] == 0.5
    assert payload["ram_percent"] == 7.5


@pytest.mark.asyncio
async def test_ohne_haos_sind_cpu_ram_disk_null_aber_ha_version_da():
    """Nicht-HAOS-Setup: CPU/RAM/Disk = None (kein Supervisor),
    aber ha_version + entities_count müssen trotzdem ankommen."""
    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["ha_version"] == "2026.5.0"
    assert payload["cpu_percent"] is None
    assert payload["ram_percent"] is None
    assert payload["disk_percent"] is None
    assert payload["entities_count"] == 0  # FakeHass.states.async_all() → []


# --------------------------------------------------------- Kritische Logs (#65)


class _FakeRecords:
    """Bildet HAs system_log DedupStore nach — `to_list()` neueste zuerst."""

    def __init__(self, entries: list[dict]):
        self._entries = entries

    def to_list(self) -> list[dict]:
        return list(self._entries)


class _FakeSystemLog:
    """Bildet die LogErrorHandler-Instanz aus hass.data['system_log'] nach."""

    def __init__(self, entries: list[dict]):
        self.records = _FakeRecords(entries)


def _log_entry(level: str, name: str, message: str, ts: float = 1_716_998_400.0) -> dict:
    """Ein system_log-Eintrag im Format, das `to_list()` liefert (message als Liste)."""
    return {
        "level": level,
        "name": name,
        "message": [message],
        "timestamp": ts,
        "source": ("x.py", 1),
        "exception": "",
        "count": 1,
        "first_occurred": ts,
    }


def _make_reporter(hass: FakeHass) -> StateReporter:
    return StateReporter(
        hass, "e1", FakeSession(), "https://api.ha-fleet-manager.com", "key"
    )


def test_collect_error_logs_filtert_nur_error_und_critical():
    """Spec #65: nur ERROR/CRITICAL — WARNING/INFO werden verworfen."""
    hass = FakeHass()
    hass.data["system_log"] = _FakeSystemLog([
        _log_entry("ERROR", "homeassistant.components.knx", "knx down"),
        _log_entry("WARNING", "homeassistant.components.mqtt", "mqtt slow"),
        _log_entry("CRITICAL", "custom_components.frigate.api", "frigate dead"),
        _log_entry("INFO", "homeassistant.core", "started"),
    ])

    logs = _make_reporter(hass)._collect_error_logs()

    assert len(logs) == 2
    assert {entry["level"] for entry in logs} == {"ERROR", "CRITICAL"}
    assert logs[0]["source"] == "knx"
    assert logs[0]["message"] == "knx down"
    assert logs[1]["source"] == "frigate"


def test_collect_error_logs_leer_ohne_system_log_komponente():
    """Wenn die system_log-Komponente fehlt, liefert die Methode []."""
    assert _make_reporter(FakeHass())._collect_error_logs() == []


def test_collect_error_logs_kuerzt_lange_message():
    """Lange Messages werden auf MAX_ERROR_LOG_MESSAGE_LEN (500) gekuerzt."""
    hass = FakeHass()
    hass.data["system_log"] = _FakeSystemLog([
        _log_entry("ERROR", "homeassistant.core", "x" * 1000),
    ])

    logs = _make_reporter(hass)._collect_error_logs()

    assert len(logs[0]["message"]) == 500


def test_collect_error_logs_begrenzt_anzahl():
    """Mehr als MAX_ERROR_LOGS (50) Eintraege werden defensiv gekappt."""
    hass = FakeHass()
    hass.data["system_log"] = _FakeSystemLog([
        _log_entry("ERROR", "homeassistant.core", f"err {i}") for i in range(80)
    ])

    logs = _make_reporter(hass)._collect_error_logs()

    assert len(logs) == 50


def test_collect_warning_logs_filtert_nur_warning():
    """Warnungs-Pipeline: nur WARNING — ERROR/CRITICAL/INFO werden verworfen."""
    hass = FakeHass()
    hass.data["system_log"] = _FakeSystemLog([
        _log_entry("ERROR", "homeassistant.components.knx", "knx down"),
        _log_entry("WARNING", "homeassistant.components.mqtt", "mqtt slow"),
        _log_entry("CRITICAL", "custom_components.frigate.api", "frigate dead"),
        _log_entry("INFO", "homeassistant.core", "started"),
    ])

    logs = _make_reporter(hass)._collect_warning_logs()

    assert len(logs) == 1
    assert logs[0]["level"] == "WARNING"
    assert logs[0]["source"] == "mqtt"
    assert logs[0]["message"] == "mqtt slow"


def test_warnings_verdraengen_fehler_nicht():
    """Architektur-Kern: Fehler und Warnungen haben getrennte Limits.

    Viele Warnungen duerfen die selteneren ERROR/CRITICAL-Eintraege nicht aus
    dem Snapshot draengen — getrennte Pipelines, getrennte 50er-Limits.
    """
    hass = FakeHass()
    entries = [_log_entry("WARNING", "homeassistant.core", f"warn {i}") for i in range(80)]
    entries.append(_log_entry("ERROR", "homeassistant.components.knx", "knx down"))
    hass.data["system_log"] = _FakeSystemLog(entries)

    reporter = _make_reporter(hass)
    errors = reporter._collect_error_logs()
    warnings = reporter._collect_warning_logs()

    # Fehler bleibt erhalten, obwohl 80 Warnungen davor im Puffer liegen.
    assert len(errors) == 1
    assert errors[0]["source"] == "knx"
    # Warnungen werden unabhaengig auf MAX_WARNING_LOGS (50) gekappt.
    assert len(warnings) == 50


def test_collect_warning_logs_leer_ohne_system_log_komponente():
    """Wenn die system_log-Komponente fehlt, liefert die Methode []."""
    assert _make_reporter(FakeHass())._collect_warning_logs() == []


@pytest.mark.parametrize(
    "name,expected",
    [
        ("homeassistant.components.knx.climate", "knx"),
        ("homeassistant.components.knx", "knx"),
        ("custom_components.frigate.api", "frigate"),
        ("homeassistant.core", "core"),
        ("homeassistant", "core"),
        ("homeassistant.setup", "setup"),
        ("zigbee2mqtt.adapter", "zigbee2mqtt"),
        ("", "?"),
        (None, "?"),
    ],
)
def test_shorten_logger(name, expected):
    assert StateReporter._shorten_logger(name) == expected


def test_epoch_to_iso():
    """Epoch-Sekunden -> ISO-8601-UTC mit Z-Suffix; robust gegen Murks."""
    assert StateReporter._epoch_to_iso(0) == "1970-01-01T00:00:00Z"
    assert StateReporter._epoch_to_iso(None) is None
    assert StateReporter._epoch_to_iso("nonsense") is None


@pytest.mark.asyncio
async def test_payload_enthaelt_error_logs_und_errors_count():
    """End-to-end: error_logs + warning_logs landen getrennt im Payload."""
    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"
    hass.data["system_log"] = _FakeSystemLog([
        _log_entry("ERROR", "homeassistant.components.knx", "knx down"),
        _log_entry("CRITICAL", "custom_components.frigate.api", "frigate dead"),
        _log_entry("WARNING", "homeassistant.components.mqtt", "mqtt slow"),
    ])

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["errors"] == 2
    assert len(payload["error_logs"]) == 2
    assert payload["error_logs"][0]["source"] == "knx"
    assert payload["error_logs"][0]["level"] == "ERROR"
    assert payload["error_logs"][0]["at"].endswith("Z")
    # Warnungen getrennt: WARNING landet ausschliesslich in warning_logs.
    assert len(payload["warning_logs"]) == 1
    assert payload["warning_logs"][0]["level"] == "WARNING"
    assert payload["warning_logs"][0]["source"] == "mqtt"


@pytest.mark.asyncio
async def test_errors_ist_null_ohne_fehler_logs():
    """Ohne system_log-Komponente: errors = 0, error_logs = []."""
    session = FakeSession(response_status=200)
    hass = FakeHass()
    hass.config.version = "2026.5.0"

    reporter = StateReporter(
        hass, "e1", session, "https://api.ha-fleet-manager.com", "key"
    )
    with patch.object(reporter, "_fetch_supervisor_info", return_value=(None, None, None)):
        await reporter._push_once()

    payload = session.calls[0]["json"]
    assert payload["errors"] == 0
    assert payload["error_logs"] == []
    assert payload["warning_logs"] == []
