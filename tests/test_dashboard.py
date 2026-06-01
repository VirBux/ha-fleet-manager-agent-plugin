"""Tests fuer das Auto-Dashboard 'Fernwartung' (REQUIREMENTS Sec. 4.6 / TODO #91).

Drei Bloecke:

1. ``build_dashboard_config`` — reiner Dict-Builder (deterministisch, kein HA).
2. ``_resolve_entity_ids`` — Aufloesung ueber die Entity-Registry per
   ``unique_id`` (NICHT per Slug-Raten).
3. ``async_ensure_dashboard`` / ``async_remove_dashboard`` — Lebenszyklus
   gegen ein gestubbtes ``LovelaceData`` + ``frontend``-Panel-API.

Architektur (REQUIREMENTS Sec. 4.6): das Plugin haengt eine eigene
``LovelaceStorage`` direkt in ``hass.data['lovelace'].dashboards`` ein und
registriert das Sidebar-Panel ueber ``frontend.async_register_built_in_panel``.
HAs interne ``DashboardsCollection`` wird nicht angefasst (parallele
Store-Writes wuerden sich gegenseitig ueberschreiben). Tests pruefen genau
diesen Pfad.

Hinweis zum Store: ``dashboard._dashboard_store`` erzeugt pro Aufruf einen
neuen ``Store``-Stub. Der ``_StoreStub`` aus ``conftest`` persistiert nur pro
Instanz. Wir monkeypatchen ``_dashboard_store`` daher in den Tests auf ein
geteiltes Store-Objekt, damit ``async_ensure_dashboard`` und nachfolgende
Abfragen denselben Zustand sehen.
"""

from __future__ import annotations

import asyncio

import pytest

from ha_fleet_agent import dashboard
from ha_fleet_agent.const import DOMAIN
from ha_fleet_agent.dashboard import (
    DASHBOARD_ICON,
    DASHBOARD_TITLE,
    DASHBOARD_URL_PATH,
    ENTITY_SLOTS,
    async_ensure_dashboard,
    async_remove_dashboard,
    build_dashboard_config,
)


# --------------------------------------------------------- Fakes


class _FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


class _FakeLovelaceData:
    """Bildet die zwei Attribute ab, die unser Code defensiv nutzt.

    Nur ``dashboards`` ist relevant — ``resources``/``yaml_dashboards`` etc.
    fasst dashboard.py nicht an.
    """

    def __init__(self) -> None:
        self.dashboards: dict = {}


class _FakeHass:
    """Schlanker HA-Stub mit ``data``-dict.

    ``hass.async_create_task`` muss synchron das Coroutine ausfuehren, sonst
    laeuft die initiale Karten-Speicherung (``_save_initial_config``) nicht
    fertig, bevor der Test prueft.
    """

    def __init__(self, lovelace: _FakeLovelaceData | None = None) -> None:
        self.data: dict = {}
        if lovelace is not None:
            self.data["lovelace"] = lovelace
        self._tasks: list = []

    def async_create_task(self, coro):
        # Synchron in einer asyncio.Task einplanen — der Test awaitet danach.
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        return task

    async def drain_tasks(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)
        self._tasks.clear()


def _install_entities(entry_id: str) -> None:
    """Registriert alle Plugin-Entities im Stub-Registry mit deterministischen IDs."""
    from homeassistant.helpers import entity_registry as er

    registry = er._singleton  # type: ignore[attr-defined]
    for slot, platform, suffix in ENTITY_SLOTS:
        registry.register(platform, DOMAIN, f"{entry_id}{suffix}", f"{platform}.fa_{slot}")


def _reset_registry() -> None:
    from homeassistant.helpers import entity_registry as er

    er._singleton._entries.clear()  # type: ignore[attr-defined]


def _frontend_calls() -> list[dict]:
    from homeassistant.components import frontend

    return frontend._test_calls  # type: ignore[attr-defined]


def _reset_frontend() -> None:
    _frontend_calls().clear()


class _MemoryStore:
    """Geteilter Store, der zwischen mehreren _dashboard_store(...)-Aufrufen
    denselben Zustand haelt."""

    def __init__(self) -> None:
        self._data: dict | None = None

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data

    async def async_remove(self):
        self._data = None


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Vor jedem Test: Entity-Registry leeren, Frontend-Calls leeren,
    geteilten Store installieren, UUID-Generierung deterministisch machen."""
    _reset_registry()
    _reset_frontend()
    shared = _MemoryStore()
    monkeypatch.setattr(dashboard, "_dashboard_store", lambda _hass: shared)
    # uuid.uuid4().hex deterministisch → einfacher zu pruefen
    counter = {"n": 0}

    class _FakeUuid:
        def __init__(self, hex_value: str) -> None:
            self.hex = hex_value

    def _fake_uuid4():
        counter["n"] += 1
        return _FakeUuid(f"uuid{counter['n']:04x}")

    monkeypatch.setattr(dashboard.uuid, "uuid4", _fake_uuid4)
    yield shared


# --------------------------------------------------------- Builder-Tests


def test_builder_alle_entities_vorhanden_erzeugt_alle_sektionen():
    entity_ids = {slot: f"{platform}.fa_{slot}" for slot, platform, _ in ENTITY_SLOTS}

    cfg = build_dashboard_config(entity_ids)

    assert cfg["title"] == DASHBOARD_TITLE
    view = cfg["views"][0]
    assert view["type"] == "sections"
    assert view["icon"] == DASHBOARD_ICON
    assert view["max_columns"] == 2

    # Header-Markdown ueber allen Sektionen (Titel der Seite)
    header_card = view["header"]["card"]
    assert header_card["type"] == "markdown"
    assert header_card["text_only"] is True
    assert "Fernwartung" in header_card["content"]

    sections = view["sections"]
    # Kopf-Erklaerung + 3 Sektionen (Status, Steuerung, Aktionen)
    assert len(sections) == 4

    # Kopf-Sektion: breit (column_span 2), Background-Akzent, eine Markdown-Karte
    head = sections[0]
    assert head["column_span"] == 2
    assert head["background"] == {"opacity": 50}
    assert len(head["cards"]) == 1
    assert head["cards"][0]["type"] == "markdown"
    assert head["cards"][0]["grid_options"] == {"columns": "full"}
    intro = head["cards"][0]["content"]
    # Beantwortet die drei Leitfragen, die der Endkunde stellen koennte
    assert "Was ist dieses Dashboard?" in intro
    assert "Wozu brauche ich es?" in intro
    assert "Was muss ich tun?" in intro
    assert "HA Fleet Agent" in intro  # Herkunft transparent

    # Die drei Sub-Sektionen: jeweils Heading + Erklaer-Markdown + Tiles
    headings = [s["cards"][0]["heading"] for s in sections[1:]]
    assert headings == ["Status", "Vorab-Freigabe (Steuerung)", "Aktionen"]

    for section in sections[1:]:
        assert section["cards"][0]["type"] == "heading"
        assert section["cards"][1]["type"] == "markdown"
        # Erklaer-Markdown spannt sich ueber die ganze Sektionsbreite
        assert section["cards"][1]["grid_options"] == {"columns": "full"}

    status_tiles = [c for c in sections[1]["cards"] if c["type"] == "tile"]
    assert len(status_tiles) == 5
    control_tiles = [c for c in sections[2]["cards"] if c["type"] == "tile"]
    assert len(control_tiles) == 3
    action_tiles = [c for c in sections[3]["cards"] if c["type"] == "tile"]
    assert len(action_tiles) == 1


def test_builder_ueberspringt_fehlende_entity():
    entity_ids = {slot: f"sensor.fa_{slot}" for slot, _, _ in ENTITY_SLOTS}
    entity_ids["preauth_expires_at"] = None  # fehlt

    cfg = build_dashboard_config(entity_ids)
    status_section = cfg["views"][0]["sections"][1]
    tile_entities = [c["entity"] for c in status_section["cards"] if c["type"] == "tile"]
    assert "sensor.fa_preauth_expires_at" not in tile_entities
    assert len(tile_entities) == 4  # 5 - 1 fehlt


def test_builder_leere_sektion_entfaellt_komplett():
    entity_ids = {slot: None for slot, _, _ in ENTITY_SLOTS}
    # Nur Status-Sensor da
    entity_ids["remote_access_status"] = "sensor.fa_remote"

    cfg = build_dashboard_config(entity_ids)
    sections = cfg["views"][0]["sections"]
    # Kopf + nur Status-Sektion (Steuerung und Aktionen leer -> weg)
    assert len(sections) == 2
    # Status-Sektion: Heading an Position 0, Erklaer-Markdown an Position 1
    status_section = sections[1]
    assert status_section["cards"][0]["heading"] == "Status"
    assert status_section["cards"][1]["type"] == "markdown"


def test_builder_alle_entities_fehlen_zeigt_nur_kopf_erklaerung():
    entity_ids = {slot: None for slot, _, _ in ENTITY_SLOTS}

    cfg = build_dashboard_config(entity_ids)
    sections = cfg["views"][0]["sections"]
    assert len(sections) == 1
    # Kopf-Erklaer-Sektion bleibt erhalten, auch ohne Entities
    assert sections[0]["cards"][0]["type"] == "markdown"
    assert sections[0]["column_span"] == 2


# --------------------------------------------------------- Resolve-Tests


def test_resolve_entity_ids_loest_alle_slots_auf():
    _install_entities("entry-1")

    resolved = dashboard._resolve_entity_ids(_FakeHass(), "entry-1")

    assert resolved["remote_access_status"] == "sensor.fa_remote_access_status"
    assert resolved["tunnel_active"] == "binary_sensor.fa_tunnel_active"
    assert resolved["pre_authorization"] == "switch.fa_pre_authorization"
    assert resolved["close_tunnel"] == "button.fa_close_tunnel"


def test_resolve_entity_ids_unbekannte_entity_ist_none():
    # Entity-Registry leer
    resolved = dashboard._resolve_entity_ids(_FakeHass(), "entry-1")
    assert all(v is None for v in resolved.values())


# --------------------------------------------------------- ensure-Tests


@pytest.mark.asyncio
async def test_ensure_legt_dashboard_an_und_setzt_flag(_clean_state):
    _install_entities("entry-1")
    lovelace = _FakeLovelaceData()
    hass = _FakeHass(lovelace)

    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()

    # LovelaceStorage haengt unter unserem url_path
    assert DASHBOARD_URL_PATH in lovelace.dashboards
    storage_obj = lovelace.dashboards[DASHBOARD_URL_PATH]
    assert storage_obj.config["url_path"] == DASHBOARD_URL_PATH
    assert storage_obj.config["title"] == DASHBOARD_TITLE
    assert storage_obj.config["id"] == "uuid0001"
    assert storage_obj.config["mode"] == "storage"

    # Karten-Save lief und enthaelt die Tiles fuer alle Slots
    assert storage_obj.saved is not None
    assert storage_obj.saved["title"] == DASHBOARD_TITLE
    sections = storage_obj.saved["views"][0]["sections"]
    status_tiles = [c for c in sections[1]["cards"] if c["type"] == "tile"]
    assert status_tiles[0]["entity"] == "sensor.fa_remote_access_status"

    # Frontend-Panel registriert
    register_calls = [c for c in _frontend_calls() if c["action"] == "register"]
    assert len(register_calls) == 1
    panel = register_calls[0]
    assert panel["component"] == "lovelace"
    assert panel["frontend_url_path"] == DASHBOARD_URL_PATH
    assert panel["sidebar_title"] == DASHBOARD_TITLE
    assert panel["sidebar_icon"] == DASHBOARD_ICON
    assert panel["require_admin"] is False
    assert panel["config"] == {"mode": "storage"}

    # Flag gesetzt mit dashboard_id
    flag = await _clean_state.async_load()
    assert flag == {
        "created": True,
        "url_path": DASHBOARD_URL_PATH,
        "dashboard_id": "uuid0001",
        "template_version": 1,
    }


@pytest.mark.asyncio
async def test_ensure_haengt_storage_nach_neustart_wieder_ein(_clean_state):
    """Nach HA-Neustart: Flag steht, LovelaceData ist aber leer (HA hat unser
    Dashboard ja nicht in seiner DashboardsCollection)."""
    _install_entities("entry-1")
    await _clean_state.async_save(
        {
            "created": True,
            "url_path": DASHBOARD_URL_PATH,
            "dashboard_id": "uuid-from-prev",
            "template_version": 1,
        }
    )
    lovelace = _FakeLovelaceData()  # leer — nach Neustart
    hass = _FakeHass(lovelace)

    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()

    # LovelaceStorage haengt wieder mit DERSELBEN dashboard_id
    storage_obj = lovelace.dashboards[DASHBOARD_URL_PATH]
    assert storage_obj.config["id"] == "uuid-from-prev"
    # KEIN Karten-Save — Storage hat seine Karten bereits aus dem
    # lovelace.<id>-Store (im echten HA)
    assert storage_obj.saved is None

    # Frontend-Panel wieder registriert
    register_calls = [c for c in _frontend_calls() if c["action"] == "register"]
    assert len(register_calls) == 1


@pytest.mark.asyncio
async def test_ensure_nichts_zu_tun_wenn_dashboard_schon_haengt(_clean_state):
    """Setup laeuft zum zweiten Mal in derselben Session — alles bleibt."""
    _install_entities("entry-1")
    await _clean_state.async_save(
        {
            "created": True,
            "url_path": DASHBOARD_URL_PATH,
            "dashboard_id": "uuid-stable",
            "template_version": 1,
        }
    )
    lovelace = _FakeLovelaceData()
    # Stelle existierendes Storage rein
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    existing = LovelaceStorage(None, {"id": "uuid-stable", "url_path": DASHBOARD_URL_PATH})
    lovelace.dashboards[DASHBOARD_URL_PATH] = existing
    hass = _FakeHass(lovelace)

    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()

    # Unangetastet
    assert lovelace.dashboards[DASHBOARD_URL_PATH] is existing
    assert _frontend_calls() == []


@pytest.mark.asyncio
async def test_ensure_respektiert_fremd_dashboard_und_speichert_flag_ohne_id(
    _clean_state,
):
    """Kunde hat unter url_path 'ha-fleet-manager' bereits etwas angelegt —
    nicht ueberschreiben, Flag mit dashboard_id=None setzen."""
    _install_entities("entry-1")
    lovelace = _FakeLovelaceData()
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    fremd = LovelaceStorage(None, {"id": "fremd-id", "url_path": DASHBOARD_URL_PATH})
    fremd.saved = {"existing": True}
    lovelace.dashboards[DASHBOARD_URL_PATH] = fremd
    hass = _FakeHass(lovelace)

    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()

    # Fremd-Dashboard unangetastet
    assert lovelace.dashboards[DASHBOARD_URL_PATH] is fremd
    assert fremd.saved == {"existing": True}
    # KEIN Panel-Register (wir machen nichts neu)
    assert _frontend_calls() == []
    # Flag aber gesetzt — naechster Lauf geht direkt raus
    flag = await _clean_state.async_load()
    assert flag == {
        "created": True,
        "url_path": DASHBOARD_URL_PATH,
        "dashboard_id": None,
        "template_version": 1,
    }


@pytest.mark.asyncio
async def test_ensure_macht_nichts_wenn_lovelace_data_fehlt(_clean_state):
    """LovelaceData noch nicht angelegt (sehr frueher Setup) — Flag bleibt
    leer, async_at_started-Fallback im Caller versucht es spaeter erneut."""
    _install_entities("entry-1")
    hass = _FakeHass(None)

    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()

    assert (await _clean_state.async_load()) is None
    assert _frontend_calls() == []


@pytest.mark.asyncio
async def test_ensure_flag_ohne_id_bleibt_no_op(_clean_state):
    """Frueheres Setup hat ein Fremd-Dashboard respektiert (dashboard_id=None).
    Bei jedem weiteren Lauf passiert nichts — nicht versuchen, doch ein
    eigenes daneben zu erzeugen."""
    _install_entities("entry-1")
    await _clean_state.async_save(
        {
            "created": True,
            "url_path": DASHBOARD_URL_PATH,
            "dashboard_id": None,
            "template_version": 1,
        }
    )
    lovelace = _FakeLovelaceData()  # auch leer
    hass = _FakeHass(lovelace)

    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()

    assert lovelace.dashboards == {}
    assert _frontend_calls() == []


# --------------------------------------------------------- remove-Tests


@pytest.mark.asyncio
async def test_remove_loescht_panel_storage_und_flag(_clean_state):
    """Vollstaendige Entfernung — Panel weg, Storage geloescht, Flag weg."""
    _install_entities("entry-1")
    lovelace = _FakeLovelaceData()
    hass = _FakeHass(lovelace)
    await async_ensure_dashboard(hass, _FakeEntry("entry-1"))
    await hass.drain_tasks()
    storage_obj = lovelace.dashboards[DASHBOARD_URL_PATH]
    _reset_frontend()  # nur die Remove-Aufrufe interessieren

    await async_remove_dashboard(hass, _FakeEntry("entry-1"))

    # Panel entfernt
    assert _frontend_calls() == [
        {"action": "remove", "url_path": DASHBOARD_URL_PATH}
    ]
    # Storage aus LovelaceData
    assert DASHBOARD_URL_PATH not in lovelace.dashboards
    # LovelaceStorage.async_delete wurde aufgerufen
    assert storage_obj.deleted is True
    # Flag weg
    assert (await _clean_state.async_load()) is None


@pytest.mark.asyncio
async def test_remove_ohne_dashboard_loescht_nur_flag(_clean_state):
    """Es gab nie eins — Panel-Remove laeuft trotzdem (no-op im Stub), kein Crash."""
    await _clean_state.async_save(
        {
            "created": True,
            "url_path": DASHBOARD_URL_PATH,
            "dashboard_id": None,
            "template_version": 1,
        }
    )
    lovelace = _FakeLovelaceData()
    hass = _FakeHass(lovelace)

    await async_remove_dashboard(hass, _FakeEntry("entry-1"))

    assert (await _clean_state.async_load()) is None
    # Panel-Remove wird trotzdem versucht (defensiv)
    assert _frontend_calls() == [
        {"action": "remove", "url_path": DASHBOARD_URL_PATH}
    ]


@pytest.mark.asyncio
async def test_remove_ohne_lovelace_data_loescht_nur_flag(_clean_state):
    await _clean_state.async_save(
        {
            "created": True,
            "url_path": DASHBOARD_URL_PATH,
            "dashboard_id": "uuid-x",
            "template_version": 1,
        }
    )
    hass = _FakeHass(None)

    await async_remove_dashboard(hass, _FakeEntry("entry-1"))

    assert (await _clean_state.async_load()) is None
