"""Tests für den BackupRequestHandler (Poll-Aktion ``backup_create``, #168).

Ohne echtes Home Assistant: Ein Fake-Backup-Manager bildet die genutzte API des
HA-Backup-Managers nach (Zustand, Agents, Konfiguration, ``async_initiate_backup`` mit
Ereignissen, Liste, Download, Löschen), ein Fake-Backend den Server mit seinem Stand je
Auftrag.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ha_fleet_agent import backup_handler as backup_module
from ha_fleet_agent.backup_handler import BackupRequestHandler

REQUEST_ID = "7f1c2d3e-0000-4000-8000-000000000001"
CHUNK = 1000


@pytest.fixture(autouse=True)
def _keine_pausen(monkeypatch):
    """Backoff, Wartezeiten und Wiederholungsfristen im Test auf null setzen."""
    monkeypatch.setattr(backup_module, "BACKUP_CHUNK_BACKOFF_SECONDS", (0, 0, 0, 0))
    monkeypatch.setattr(backup_module, "BACKUP_REPORT_BACKOFF_SECONDS", (0, 0))
    monkeypatch.setattr(backup_module, "BACKUP_RESUME_RETRY_SECONDS", 0)
    monkeypatch.setattr(backup_module, "BACKUP_DEFAULT_RETRY_AFTER_SECONDS", 0)


# ============================================================ Fake-Backup-Manager


@dataclass
class FakeBackup:
    backup_id: str
    size: int
    protected: bool
    extra_metadata: dict[str, Any]
    content: bytes
    date: str = ""


@dataclass
class FakeEvent:
    state: str
    reason: str | None = None
    manager_state: str = "create_backup"


@dataclass
class FakeCreateConfig:
    password: str | None = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ12"
    include_database: bool = True
    include_all_addons: bool = False
    include_addons: list[str] | None = None
    include_folders: list[str] | None = None


@dataclass
class FakeAgentConfig:
    protected: bool


@dataclass
class FakeConfigData:
    create_backup: FakeCreateConfig = field(default_factory=FakeCreateConfig)
    agents: dict[str, FakeAgentConfig] = field(default_factory=dict)


class FakeConfig:
    def __init__(self) -> None:
        self.data = FakeConfigData()


class FakeAgent:
    """Lokaler Agent. ``path_dir`` gesetzt = Core (Datei), sonst HA OS (Stream)."""

    def __init__(self, path_dir: Path | None = None) -> None:
        self.backups: dict[str, FakeBackup] = {}
        self._path_dir = path_dir
        self.download_calls = 0

    async def async_list_backups(self) -> list[FakeBackup]:
        return list(self.backups.values())

    def get_backup_path(self, backup_id: str) -> Path:
        path = self._path_dir / f"{backup_id}.tar"
        path.write_bytes(self.backups[backup_id].content)
        return path

    async def async_download_backup(self, backup_id: str):
        self.download_calls += 1
        content = self.backups[backup_id].content

        async def _stream():
            # Bewusst ungerade Stückelung — wie ein echter HTTP-Stream.
            for i in range(0, len(content), 777):
                yield content[i:i + 777]

        return _stream()


class FakeManager:
    def __init__(self, *, supervised: bool = True, tmp: Path | None = None,
                 content: bytes = b"", protected: bool = True, fail_create: bool = False) -> None:
        self.state = "idle"
        self.config = FakeConfig()
        self.local_id = "hassio.local" if supervised else "backup.local"
        self.agent = FakeAgent(None if supervised else tmp)
        self.backup_agents: dict[str, FakeAgent] = {self.local_id: self.agent}
        self.local_backup_agents: dict[str, FakeAgent] = {} if supervised else {self.local_id: self.agent}
        self.content = content
        self.protected = protected
        self.fail_create = fail_create
        self.initiate_calls: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, list[str]]] = []
        self._subscribers: list[Any] = []

    def async_subscribe_events(self, callback):
        self._subscribers.append(callback)

        def _remove() -> None:
            self._subscribers.remove(callback)

        return _remove

    async def async_initiate_backup(self, **kwargs: Any):
        self.initiate_calls.append(kwargs)
        loop = asyncio.get_running_loop()
        backup = FakeBackup(
            backup_id=f"bk{len(self.initiate_calls)}",
            size=len(self.content),
            protected=self.protected,
            extra_metadata=dict(kwargs.get("extra_metadata") or {}),
            content=self.content,
            date=f"2026-09-27T12:{len(self.initiate_calls):02d}:00+02:00",
        )

        def _finish() -> None:
            for cb in list(self._subscribers):
                cb(FakeEvent(state="in_progress"))
            if self.fail_create:
                for cb in list(self._subscribers):
                    cb(FakeEvent(state="failed", reason="upload_failed"))
                return
            self.agent.backups[backup.backup_id] = backup
            for cb in list(self._subscribers):
                cb(FakeEvent(state="completed"))

        loop.call_soon(_finish)
        return object()

    async def async_delete_backup(self, backup_id: str, *, agent_ids: list[str] | None = None):
        self.deleted.append((backup_id, list(agent_ids or [])))
        self.agent.backups.pop(backup_id, None)
        return {}


# ============================================================ Fake-Backend


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body or {}
        self.headers = headers or {}

    async def json(self, content_type=None):  # noqa: ANN001
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeBackend:
    """Server mit Stand je Auftrag. Einzelne Antworten lassen sich vorab einplanen."""

    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.completes: list[dict[str, Any]] = []
        self.received: dict[str, bytearray] = {}
        self.status: dict[str, str] = {}
        self.put_ranges: list[str] = []
        # Einmalige Sonderantworten: Liste von (Methode, Endung, FakeResponse).
        self.scripted: list[tuple[str, str, FakeResponse]] = []

    def _scripted(self, method: str, url: str) -> FakeResponse | None:
        for i, (m, suffix, resp) in enumerate(self.scripted):
            if m == method and url.endswith(suffix):
                del self.scripted[i]
                return resp
        return None

    @staticmethod
    def _rid(url: str) -> str:
        return url.split("/backup-requests/")[1].split("/")[0]

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
        scripted = self._scripted("POST", url)
        if scripted is not None:
            return scripted
        rid = self._rid(url)
        if url.endswith("/report"):
            self.reports.append({"request_id": rid, **(json or {})})
            if self.status.get(rid) == "closed":
                return FakeResponse(410, {"error": "request_closed"})
            if json.get("status") == "uploading":
                self.status[rid] = "uploading"
                self.received.setdefault(rid, bytearray())
            return FakeResponse(204)
        if url.endswith("/complete"):
            self.completes.append({"request_id": rid, **(json or {})})
            data = bytes(self.received.get(rid, b""))
            if hashlib.sha256(data).hexdigest() != json.get("sha256") or len(data) != json.get("sizeBytes"):
                return FakeResponse(400, {"error": "sha256_mismatch"})
            self.status[rid] = "ready"
            return FakeResponse(200, {"status": "ready"})
        raise AssertionError(url)

    def put(self, url, data=None, headers=None, timeout=None):  # noqa: ANN001
        self.put_ranges.append(headers["Content-Range"])
        scripted = self._scripted("PUT", url)
        if scripted is not None:
            return scripted
        rid = self._rid(url)
        if self.status.get(rid) == "closed":
            return FakeResponse(410, {"error": "request_closed"})
        start = int(headers["Content-Range"].split(" ")[1].split("-")[0])
        buf = self.received.setdefault(rid, bytearray())
        if start != len(buf):
            return FakeResponse(409, {"error": "offset_mismatch", "receivedBytes": len(buf)})
        buf.extend(data)
        return FakeResponse(200, {"receivedBytes": len(buf)})

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001
        scripted = self._scripted("GET", url)
        if scripted is not None:
            return scripted
        rid = self._rid(url)
        status = self.status.get(rid, "uploading")
        if status == "closed":
            return FakeResponse(410, {"error": "request_closed"})
        return FakeResponse(200, {"status": status, "receivedBytes": len(self.received.get(rid, b""))})


# ============================================================ Fake-HA


class FakeStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


class FakeHass:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name):  # noqa: ANN001
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def async_add_executor_job(self, func, *args):  # noqa: ANN001
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def settle(self) -> None:
        while any(not t.done() for t in self.tasks):
            await asyncio.gather(*self.tasks, return_exceptions=True)


def _content(n: int = 3500) -> bytes:
    return bytes((i * 37 + 11) % 256 for i in range(n))


def _handler(hass: FakeHass, backend: FakeBackend, manager: FakeManager | None, *,
             ha_version=(2026, 9), store: FakeStore | None = None) -> BackupRequestHandler:
    def _getter(_hass):
        if manager is None:
            raise RuntimeError("Backup integration is not available")
        return manager

    return BackupRequestHandler(
        hass, "entry1", backend, "https://api.example", "secret",
        manager_getter=_getter, ha_version=ha_version, store=store or FakeStore(),
    )


async def _deliver(handler: BackupRequestHandler, hass: FakeHass, request_id: str = REQUEST_ID,
                   chunk: int = CHUNK, max_bytes: int = 10_000) -> None:
    await handler.handle({"action": "backup_create", "requestId": request_id,
                          "maxBytes": max_bytes, "chunkBytes": chunk})
    await hass.settle()


def _statuses(backend: FakeBackend) -> list[str]:
    return [r["status"] for r in backend.reports]


# ============================================================ Voraussetzungen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda m: None, "ha_version_unsupported"),
        (lambda m: setattr(m, "state", "create_backup"), "ha_busy"),
        (lambda m: setattr(m.config.data.create_backup, "password", None), "encryption_key_missing"),
        (lambda m: m.config.data.agents.__setitem__("hassio.local", FakeAgentConfig(protected=False)),
         "local_backups_unencrypted"),
        (lambda m: m.backup_agents.clear(), "local_agent_missing"),
    ],
)
async def test_precheck_failures_report_code_and_create_nothing(setup, code):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    setup(manager)
    version = (2025, 7) if code == "ha_version_unsupported" else (2026, 9)
    handler = _handler(hass, backend, manager, ha_version=version)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["failed"]
    assert backend.reports[0]["error"].startswith(code)
    assert manager.initiate_calls == []


@pytest.mark.asyncio
async def test_missing_backup_integration_counts_as_unsupported_ha():
    hass, backend = FakeHass(), FakeBackend()
    handler = _handler(hass, backend, None)

    await _deliver(handler, hass)

    assert backend.reports[0]["status"] == "failed"
    assert backend.reports[0]["error"].startswith("ha_version_unsupported")


# ============================================================ Erfolg


@pytest.mark.asyncio
async def test_success_on_ha_os_streams_chunks_completes_and_keeps_backup_in_ha():
    hass, backend = FakeHass(), FakeBackend()
    content = _content(3500)
    manager = FakeManager(supervised=True, content=content)
    manager.config.data.create_backup.include_all_addons = True
    manager.config.data.create_backup.include_addons = ["core_mosquitto"]
    manager.config.data.create_backup.include_folders = ["share"]
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "uploading"]
    assert backend.reports[1]["sizeBytes"] == len(content)
    assert backend.put_ranges == ["bytes 0-999/3500", "bytes 1000-1999/3500",
                                  "bytes 2000-2999/3500", "bytes 3000-3499/3500"]
    assert bytes(backend.received[REQUEST_ID]) == content
    assert backend.completes[0]["sha256"] == hashlib.sha256(content).hexdigest()
    # Umfang wie die automatischen Backups; „alle Add-ons“ schließt die Einzelliste aus.
    call = manager.initiate_calls[0]
    assert call["agent_ids"] == ["hassio.local"]
    assert call["include_all_addons"] is True
    assert call["include_addons"] is None
    assert call["include_folders"] == ["share"]
    assert call["password"] == manager.config.data.create_backup.password
    assert call["extra_metadata"] == {"fleet_agent.request_id": REQUEST_ID}
    # Die Kopie bleibt in HA (#201).
    assert manager.deleted == []
    assert "bk1" in manager.agent.backups
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
async def test_success_on_core_reads_the_file_and_excludes_addons(tmp_path):
    hass, backend = FakeHass(), FakeBackend()
    content = _content(2500)
    manager = FakeManager(supervised=False, tmp=tmp_path, content=content)
    manager.config.data.create_backup.include_all_addons = True
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert bytes(backend.received[REQUEST_ID]) == content
    call = manager.initiate_calls[0]
    assert call["agent_ids"] == ["backup.local"]
    # Auf Core/Container erlaubt HA weder Add-ons noch Ordner.
    assert call["include_all_addons"] is False
    assert call["include_addons"] is None
    assert call["include_folders"] is None
    assert manager.agent.download_calls == 0
    assert manager.deleted == []


# ============================================================ Fehler nach dem Erzeugen


@pytest.mark.asyncio
async def test_unencrypted_backup_is_deleted_and_reported():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content(), protected=False)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "failed"]
    assert backend.reports[1]["error"] == "backup_not_encrypted"
    assert manager.deleted == [("bk1", ["hassio.local"])]
    assert backend.put_ranges == []


@pytest.mark.asyncio
async def test_unencrypted_backup_is_never_stored_as_uploading():
    # Ein gespeicherter Auftrag in Phase „uploading“ muss ein geprüft verschlüsseltes Backup
    # tragen — sonst behielte der Neustart-Pfad ein ungeprüftes Backup als das laufende.
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content(), protected=False)
    phases: list[str | None] = []

    class RecordingStore(FakeStore):
        async def async_save(self, data):
            phases.append((data.get("job") or {}).get("phase"))
            await super().async_save(data)

    handler = _handler(hass, backend, manager, store=RecordingStore())

    await _deliver(handler, hass)

    assert "uploading" not in phases
    assert manager.deleted == [("bk1", ["hassio.local"])]


@pytest.mark.asyncio
async def test_too_large_backup_is_reported_and_stays_in_ha():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content(5000))
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass, max_bytes=4000)

    assert backend.reports[-1]["error"].startswith("backup_too_large")
    assert manager.deleted == []
    assert "bk1" in manager.agent.backups


@pytest.mark.asyncio
async def test_failed_creation_is_reported():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content(), fail_create=True)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "failed"]
    assert backend.reports[1]["error"].startswith("create_failed")


@pytest.mark.asyncio
async def test_rejected_reservation_ends_without_own_failure_report():
    # Das Backend hat den Auftrag bei 409 backup_staging_full selbst beendet.
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("POST", "/report", FakeResponse(204)))  # creating
    backend.scripted.append(("POST", "/report", FakeResponse(409, {"error": "backup_staging_full"})))
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert backend.put_ranges == []
    assert backend.reports == []  # beide Meldungen liefen über die Skriptantworten
    assert manager.deleted == []
    assert handler._find_done(REQUEST_ID)["status"] == "closed"


# ============================================================ Upload-Robustheit


@pytest.mark.asyncio
async def test_chunk_is_retried_after_server_error():
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("PUT", "/content", FakeResponse(502)))
    content = _content(2500)
    manager = FakeManager(content=content)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert backend.put_ranges[:2] == ["bytes 0-999/2500", "bytes 0-999/2500"]
    assert bytes(backend.received[REQUEST_ID]) == content
    assert backend.completes


@pytest.mark.asyncio
async def test_lost_response_of_accepted_chunk_continues_without_resend():
    # Das Stück kam an, die Antwort nicht: Der Server meldet danach offset_mismatch mit
    # genau dem Ende dieses Stücks — das gilt als Erfolg.
    hass, backend = FakeHass(), FakeBackend()
    content = _content(2500)
    manager = FakeManager(content=content)
    handler = _handler(hass, backend, manager)
    original_put = backend.put
    state = {"dropped": False}

    def put_dropping_first_answer(url, data=None, headers=None, timeout=None):  # noqa: ANN001
        resp = original_put(url, data=data, headers=headers, timeout=timeout)
        if not state["dropped"]:
            state["dropped"] = True
            return FakeResponse(504)
        return resp

    backend.put = put_dropping_first_answer

    await _deliver(handler, hass)

    assert bytes(backend.received[REQUEST_ID]) == content
    assert backend.completes


@pytest.mark.asyncio
async def test_offset_mismatch_resumes_at_server_offset():
    hass, backend = FakeHass(), FakeBackend()
    content = _content(3500)
    # Der Server hat schon 2000 Byte (etwa aus einem früheren Anlauf).
    backend.received[REQUEST_ID] = bytearray(content[:2000])
    manager = FakeManager(content=content)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert "bytes 0-999/3500" in backend.put_ranges  # erster Versuch ab 0 …
    assert backend.put_ranges[-2:] == ["bytes 2000-2999/3500", "bytes 3000-3499/3500"]  # … dann ab 2000
    assert bytes(backend.received[REQUEST_ID]) == content
    assert backend.completes[0]["sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_closed_request_stops_upload_without_failure_report_and_keeps_backup():
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("PUT", "/content", FakeResponse(410, {"error": "request_closed"})))
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "uploading"]
    assert backend.completes == []
    assert manager.deleted == []
    assert "bk1" in manager.agent.backups


@pytest.mark.asyncio
async def test_busy_upload_slots_are_waited_out():
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("PUT", "/content", FakeResponse(503, {"error": "upload_slots_exhausted"},
                                                             {"Retry-After": "0"})))
    content = _content(1500)
    manager = FakeManager(content=content)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert bytes(backend.received[REQUEST_ID]) == content


@pytest.mark.asyncio
async def test_backend_restart_during_upload_is_bridged():
    # Deploy mit einer Backend-Instanz (#241): Der laufende Request reißt ab, danach liefert
    # der Proxy 404 ohne JSON-error und 502, bis die neue Instanz healthy ist.
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("PUT", "/content", FakeResponse(502)))
    for _ in range(4):
        backend.scripted.append(("PUT", "/content", FakeResponse(404)))
    backend.scripted.append(("PUT", "/content", FakeResponse(502)))
    content = _content(2500)
    manager = FakeManager(content=content)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert bytes(backend.received[REQUEST_ID]) == content
    assert backend.completes
    assert backend.reports[-1]["status"] != "failed"


def test_chunk_retry_budget_covers_a_backend_restart():
    # Die Pausen zwischen den Versuchen eines Stücks müssen einen Backend-Neustart
    # überbrücken: Shutdown, Start und Healthcheck dauern zusammen bis etwa 60 s (#241).
    # Aus const lesen — das Fixture oben nullt die Pausen nur im Modul backup_handler.
    from ha_fleet_agent import const

    pauses = [
        const.BACKUP_CHUNK_BACKOFF_SECONDS[min(i, len(const.BACKUP_CHUNK_BACKOFF_SECONDS) - 1)]
        for i in range(const.BACKUP_CHUNK_ATTEMPTS - 1)
    ]
    assert sum(pauses) >= 90


@pytest.mark.asyncio
async def test_persistent_network_errors_fail_the_request():
    hass, backend = FakeHass(), FakeBackend()
    for _ in range(backup_module.BACKUP_CHUNK_ATTEMPTS):
        backend.scripted.append(("PUT", "/content", FakeResponse(500)))
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert backend.reports[-1]["status"] == "failed"
    assert backend.reports[-1]["error"].startswith("upload_failed")
    assert manager.deleted == []


# ============================================================ Doppelzustellung und Ablösung


@pytest.mark.asyncio
async def test_redelivery_of_running_request_is_ignored():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await handler.handle({"action": "backup_create", "requestId": REQUEST_ID, "chunkBytes": CHUNK})
    await handler.handle({"action": "backup_create", "requestId": REQUEST_ID, "chunkBytes": CHUNK})
    await hass.settle()

    assert len(manager.initiate_calls) == 1


@pytest.mark.asyncio
async def test_redelivery_of_finished_request_repeats_the_end_state():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)
    await _deliver(handler, hass)
    assert len(backend.completes) == 1

    await _deliver(handler, hass)

    assert len(manager.initiate_calls) == 1  # kein zweites Backup
    assert len(backend.completes) == 2  # Abschluss erneut gemeldet


@pytest.mark.asyncio
async def test_redelivery_of_failed_request_repeats_the_failure():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    manager.config.data.create_backup.password = None
    handler = _handler(hass, backend, manager)
    await _deliver(handler, hass)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["failed", "failed"]
    assert backend.reports[1]["error"] == "encryption_key_missing"


@pytest.mark.asyncio
async def test_new_request_supersedes_the_running_one_and_keeps_its_backup(monkeypatch):
    # Mit Platz für zwei zeigt sich, dass die Ablösung selbst nichts löscht.
    monkeypatch.setattr(backup_module, "BACKUP_KEEP_LOCAL", 2)
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)
    gate = asyncio.Event()
    original_put = backend.put

    def slow_put(url, data=None, headers=None, timeout=None):  # noqa: ANN001
        resp = original_put(url, data=data, headers=headers, timeout=timeout)

        class _Blocking(FakeResponse):
            async def __aenter__(self_inner):
                if REQUEST_ID in url:
                    await gate.wait()
                return resp

        return _Blocking(resp.status)

    backend.put = slow_put
    await handler.handle({"action": "backup_create", "requestId": REQUEST_ID, "chunkBytes": CHUNK})
    for _ in range(50):
        await asyncio.sleep(0)
        if backend.put_ranges:
            break
    assert backend.put_ranges, "erster Auftrag sollte im Upload stehen"

    second = "7f1c2d3e-0000-4000-8000-000000000002"
    await handler.handle({"action": "backup_create", "requestId": second, "chunkBytes": CHUNK})
    gate.set()
    await hass.settle()

    assert manager.deleted == []
    assert set(manager.agent.backups) == {"bk1", "bk2"}
    assert handler._find_done(REQUEST_ID)["status"] == "closed"
    assert handler._find_done(second)["status"] == "ready"


@pytest.mark.asyncio
async def test_new_request_replaces_the_previous_backup_with_default_keep():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)
    second = "7f1c2d3e-0000-4000-8000-000000000002"

    await _deliver(handler, hass)
    await _deliver(handler, hass, request_id=second)

    # N = 1: Das zweite Backup ersetzt das erste, erst nachdem es erzeugt ist.
    assert manager.deleted == [("bk1", ["hassio.local"])]
    assert set(manager.agent.backups) == {"bk2"}


# ============================================================ Neustart


@pytest.mark.asyncio
async def test_upload_resumes_after_restart_from_server_offset():
    hass, backend = FakeHass(), FakeBackend()
    content = _content(3500)
    manager = FakeManager(content=content)
    manager.agent.backups["bk7"] = FakeBackup("bk7", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "uploading"
    backend.received[REQUEST_ID] = bytearray(content[:2000])
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk7",
                               "size": len(content), "chunk_bytes": CHUNK}, "done": []})
    handler = _handler(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.put_ranges == ["bytes 2000-2999/3500", "bytes 3000-3499/3500"]
    assert bytes(backend.received[REQUEST_ID]) == content
    assert backend.completes[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert manager.deleted == []
    assert "bk7" in manager.agent.backups
    assert store.data["job"] is None


@pytest.mark.asyncio
async def test_restart_while_creating_reports_ha_restarted_and_keeps_newest_backup():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    manager.agent.backups["bk2"] = FakeBackup("bk2", 10, True, {"fleet_agent.request_id": "alt"}, b"w" * 10,
                                              "2026-09-26T08:00:00+02:00")
    # HA wurde mit dem Backup noch fertig — es ist vollständig und verschlüsselt.
    manager.agent.backups["bk3"] = FakeBackup("bk3", 10, True, {"fleet_agent.request_id": REQUEST_ID}, b"x" * 10,
                                              "2026-09-27T08:00:00+02:00")
    manager.agent.backups["fremd"] = FakeBackup("fremd", 10, True, {}, b"y" * 10)
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "creating"}, "done": []})
    handler = _handler(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.reports == [{"request_id": REQUEST_ID, "status": "failed", "error": "ha_restarted"}]
    assert manager.deleted == [("bk2", ["hassio.local"])]
    assert set(manager.agent.backups) == {"bk3", "fremd"}  # das Backup des Kunden bleibt


@pytest.mark.asyncio
async def test_restart_after_request_was_closed_keeps_backup():
    hass, backend = FakeHass(), FakeBackend()
    content = _content()
    manager = FakeManager(content=content)
    manager.agent.backups["bk9"] = FakeBackup("bk9", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "closed"
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk9",
                               "size": len(content)}, "done": []})
    handler = _handler(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.put_ranges == []
    assert manager.deleted == []
    assert handler._find_done(REQUEST_ID)["status"] == "closed"


@pytest.mark.asyncio
async def test_startup_without_job_keeps_newest_tagged_backup_only():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    manager.agent.backups["alt"] = FakeBackup("alt", 10, True, {"fleet_agent.request_id": "x"}, b"z" * 10,
                                              "2026-09-20T10:00:00+02:00")
    manager.agent.backups["neu"] = FakeBackup("neu", 10, True, {"fleet_agent.request_id": "y"}, b"n" * 10,
                                              "2026-09-25T10:00:00+02:00")
    manager.agent.backups["kunde"] = FakeBackup("kunde", 10, True, {"instance_id": "abc"}, b"k" * 10,
                                                "2026-09-01T10:00:00+02:00")
    handler = _handler(hass, backend, manager)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)

    assert manager.deleted == [("alt", ["hassio.local"])]
    assert set(manager.agent.backups) == {"neu", "kunde"}


@pytest.mark.asyncio
async def test_restart_when_request_was_already_ready_keeps_backup():
    hass, backend = FakeHass(), FakeBackend()
    content = _content()
    manager = FakeManager(content=content)
    manager.agent.backups["bk5"] = FakeBackup("bk5", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "ready"
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk5",
                               "size": len(content)}, "done": []})
    handler = _handler(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.put_ranges == []
    assert manager.deleted == []
    assert "bk5" in manager.agent.backups
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


# ============================================================ Bestand in HA (#201)


@pytest.mark.asyncio
async def test_backup_name_carries_the_download_suffix():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert re.fullmatch(r"Fleet Manager \d{4}-\d{2}-\d{2} \d{2}:\d{2}_for-download",
                        manager.initiate_calls[0]["name"])


@pytest.mark.asyncio
async def test_new_backup_replaces_older_tagged_backups_but_not_customer_ones():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    for bid, day in (("alt1", 20), ("alt2", 25)):
        manager.agent.backups[bid] = FakeBackup(bid, 10, True, {"fleet_agent.request_id": bid}, b"a" * 10,
                                                f"2026-09-{day}T10:00:00+02:00")
    manager.agent.backups["kunde"] = FakeBackup("kunde", 10, True, {}, b"k" * 10, "2026-09-26T10:00:00+02:00")
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert sorted(bid for bid, _ in manager.deleted) == ["alt1", "alt2"]
    assert set(manager.agent.backups) == {"bk1", "kunde"}


@pytest.mark.asyncio
async def test_failed_creation_leaves_existing_backups_untouched():
    # Aufgeräumt wird erst nach dem Erzeugen — sonst sänke ein Fehlschlag den Bestand auf N−1.
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content(), fail_create=True)
    manager.agent.backups["alt"] = FakeBackup("alt", 10, True, {"fleet_agent.request_id": "x"}, b"a" * 10,
                                              "2026-09-20T10:00:00+02:00")
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert manager.deleted == []
    assert "alt" in manager.agent.backups


@pytest.mark.asyncio
async def test_prune_keeps_n_newest_with_the_running_backup_first(monkeypatch):
    monkeypatch.setattr(backup_module, "BACKUP_KEEP_LOCAL", 2)
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    tag = {"fleet_agent.request_id": "x"}
    # Das laufende trägt bewusst das älteste Datum — es bleibt trotzdem und zählt mit.
    manager.agent.backups["laufend"] = FakeBackup("laufend", 1, True, tag, b"l", "2026-09-01T10:00:00+02:00")
    manager.agent.backups["b"] = FakeBackup("b", 1, True, tag, b"b", "2026-09-10T10:00:00+02:00")
    manager.agent.backups["c"] = FakeBackup("c", 1, True, tag, b"c", "2026-09-20T10:00:00+02:00")
    manager.agent.backups["klartext"] = FakeBackup("klartext", 1, False, tag, b"k", "2026-09-26T10:00:00+02:00")
    manager.agent.backups["kunde"] = FakeBackup("kunde", 1, False, {}, b"u", "2026-09-26T11:00:00+02:00")
    handler = _handler(hass, backend, manager)

    await handler._prune_local(manager, manager.local_id, keep_backup_id="laufend")

    # Neben dem laufenden bleibt nur das neueste verschlüsselte; Klartext mit Markierung geht immer.
    assert sorted(bid for bid, _ in manager.deleted) == ["b", "klartext"]
    assert set(manager.agent.backups) == {"laufend", "c", "kunde"}


@pytest.mark.asyncio
async def test_prune_error_never_blocks_the_request():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)
    original = manager.agent.async_list_backups
    calls = {"n": 0}

    async def flaky_list():
        calls["n"] += 1
        if calls["n"] == 2:  # 1. Aufruf: Backup finden, 2.: Aufräumen
            raise RuntimeError("Supervisor nicht erreichbar")
        return await original()

    manager.agent.async_list_backups = flaky_list

    await _deliver(handler, hass)

    assert handler._find_done(REQUEST_ID)["status"] == "ready"
    assert backend.completes


# ============================================================ Einstellungen und Notfallkit (#203)

KIT = {"mode": "automatic", "includeEmergencyKit": True}


def _custom(**overrides: Any) -> dict[str, Any]:
    settings = {"mode": "custom", "includeDatabase": False, "includeFolders": ["share"],
                "includeAllAddons": False, "includeAddons": ["core_mosquitto"],
                "includeEmergencyKit": False}
    settings.update(overrides)
    return settings


def _handler_203(hass: FakeHass, backend: FakeBackend, manager: FakeManager, *,
                 installed: set[str] | None = None, store: FakeStore | None = None) -> BackupRequestHandler:
    return BackupRequestHandler(
        hass, "entry1", backend, "https://api.example", "secret",
        manager_getter=lambda _hass: manager, ha_version=(2026, 9), store=store or FakeStore(),
        addons_getter=lambda _hass: installed, language="de",
    )


async def _deliver_with(handler: BackupRequestHandler, hass: FakeHass, settings: dict[str, Any] | None) -> None:
    data = {"action": "backup_create", "requestId": REQUEST_ID, "maxBytes": 10_000, "chunkBytes": CHUNK}
    if settings is not None:
        data["settings"] = settings
    await handler.handle(data)
    await hass.settle()


def _kit_notices() -> list[dict[str, Any]]:
    from homeassistant.components import persistent_notification  # Stub aus conftest

    return [c for c in persistent_notification._test_calls
            if c["action"] == "create" and "backup_emergency_kit" in str(c["notification_id"])]


@pytest.fixture
def _clear_notices():
    from homeassistant.components import persistent_notification

    persistent_notification._test_calls.clear()
    yield
    persistent_notification._test_calls.clear()


@pytest.mark.asyncio
async def test_custom_selection_on_ha_os_is_passed_to_ha_exactly():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    # Die automatischen Backups sähen anders aus — sie dürfen nicht durchschlagen.
    manager.config.data.create_backup.include_all_addons = True
    manager.config.data.create_backup.include_folders = ["media"]
    handler = _handler_203(hass, backend, manager, installed={"core_mosquitto", "core_ssh"})

    await _deliver_with(handler, hass, _custom(includeFolders=["share", "ssl"]))

    assert backend.reports[0] == {"request_id": REQUEST_ID, "status": "creating", "settingsApplied": True}
    call = manager.initiate_calls[0]
    assert call["include_database"] is False
    assert [str(f) for f in call["include_folders"]] == ["share", "ssl"]
    assert call["include_all_addons"] is False
    assert call["include_addons"] == ["core_mosquitto"]
    assert call["include_homeassistant"] is True
    assert call["password"] == manager.config.data.create_backup.password
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
async def test_custom_all_addons_passes_no_single_list():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    handler = _handler_203(hass, backend, manager, installed=set())

    await _deliver_with(handler, hass, _custom(includeAllAddons=True, includeAddons=[], includeFolders=[]))

    call = manager.initiate_calls[0]
    assert call["include_all_addons"] is True
    assert call["include_addons"] is None
    assert call["include_folders"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        _custom(includeAddons=[]),
        _custom(includeFolders=[], includeAddons=["core_ssh"]),
        _custom(includeFolders=[], includeAddons=[], includeAllAddons=True),
    ],
)
async def test_custom_folders_or_addons_on_core_fail_without_creating(tmp_path, settings):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=False, tmp=tmp_path, content=_content())
    handler = _handler_203(hass, backend, manager)

    await _deliver_with(handler, hass, settings)

    assert _statuses(backend) == ["failed"]
    assert backend.reports[0]["error"].startswith("settings_unsupported")
    assert manager.initiate_calls == []


@pytest.mark.asyncio
async def test_custom_on_core_with_database_only_works(tmp_path):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=False, tmp=tmp_path, content=_content())
    handler = _handler_203(hass, backend, manager)

    await _deliver_with(handler, hass, _custom(includeDatabase=True, includeFolders=[], includeAddons=[]))

    call = manager.initiate_calls[0]
    assert call["include_database"] is True
    assert call["include_folders"] is None
    assert call["include_addons"] is None
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
async def test_missing_addon_fails_with_its_slug_and_creates_nothing():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    handler = _handler_203(hass, backend, manager, installed={"core_ssh"})

    await _deliver_with(handler, hass, _custom(includeAddons=["core_ssh", "core_mosquitto"]))

    assert _statuses(backend) == ["failed"]
    assert backend.reports[0]["error"] == "addon_not_installed: core_mosquitto"
    assert manager.initiate_calls == []


@pytest.mark.asyncio
async def test_all_addons_together_with_single_addons_is_rejected():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    handler = _handler_203(hass, backend, manager, installed={"core_mosquitto"})

    await _deliver_with(handler, hass, _custom(includeAllAddons=True))

    assert backend.reports[0]["error"].startswith("settings_unsupported")
    assert manager.initiate_calls == []


@pytest.mark.asyncio
async def test_unknown_mode_or_folder_is_rejected():
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    handler = _handler_203(hass, backend, manager, installed={"core_mosquitto"})

    await _deliver_with(handler, hass, {"mode": "later"})
    assert backend.reports[0]["error"].startswith("settings_unsupported")

    handler2 = _handler_203(hass, backend, manager, installed={"core_mosquitto"})
    await handler2.handle({"action": "backup_create", "requestId": "rid-2", "maxBytes": 10_000,
                           "chunkBytes": CHUNK, "settings": _custom(includeFolders=["homeassistant"])})
    await hass.settle()
    assert backend.reports[-1]["error"].startswith("settings_unsupported")
    assert manager.initiate_calls == []


@pytest.mark.asyncio
async def test_poll_without_settings_keeps_the_automatic_scope(_clear_notices):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    manager.config.data.create_backup.include_folders = ["media"]
    manager.config.data.create_backup.include_database = False
    handler = _handler_203(hass, backend, manager)

    await _deliver_with(handler, hass, None)

    call = manager.initiate_calls[0]
    assert call["include_folders"] == ["media"]
    assert call["include_database"] is False
    assert all("emergencyKey" not in r for r in backend.reports)
    assert _kit_notices() == []


@pytest.mark.asyncio
async def test_key_goes_along_only_on_request_and_is_announced_in_ha(_clear_notices):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    handler = _handler_203(hass, backend, manager)

    await _deliver_with(handler, hass, KIT)

    uploading = [r for r in backend.reports if r["status"] == "uploading"]
    assert uploading[0]["emergencyKey"] == manager.config.data.create_backup.password
    assert [r for r in backend.reports if r["status"] == "creating"][0].get("emergencyKey") is None
    notices = _kit_notices()
    assert len(notices) == 1
    assert notices[0]["notification_id"] == "ha_fleet_agent_backup_emergency_kit_entry1"
    assert "Backup-Schlüssel" in notices[0]["message"]
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
async def test_rejected_key_report_shows_no_notice(_clear_notices):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    backend.scripted.append(("POST", "/report", FakeResponse(204)))  # creating
    backend.scripted.append(("POST", "/report", FakeResponse(409, {"error": "backup_staging_full"})))
    handler = _handler_203(hass, backend, manager)

    await _deliver_with(handler, hass, KIT)

    assert _kit_notices() == []
    assert handler._find_done(REQUEST_ID)["status"] == "closed"


@pytest.mark.asyncio
async def test_key_never_appears_in_log_or_store(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(supervised=True, content=_content())
    store = FakeStore()
    handler = _handler_203(hass, backend, manager, store=store)
    key = manager.config.data.create_backup.password
    # Auch ein Fehlschlag nach der Meldung darf den Schlüssel nicht ausgeben.
    backend.scripted.append(("PUT", "/content", FakeResponse(400, {"error": "bad"})))

    await _deliver_with(handler, hass, KIT)

    assert key not in caplog.text
    assert key not in repr(store.data)
    assert any(r.get("emergencyKey") == key for r in backend.reports)


@pytest.mark.asyncio
async def test_restart_before_uploading_report_resends_it_with_the_key(_clear_notices):
    hass, backend = FakeHass(), FakeBackend()
    content = _content(2500)
    manager = FakeManager(content=content)
    key = manager.config.data.create_backup.password
    manager.agent.backups["bk7"] = FakeBackup("bk7", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "creating"
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk7",
                               "size": len(content), "chunk_bytes": CHUNK,
                               "settings": backup_module._parse_settings(KIT),
                               "key_sha256": hashlib.sha256(key.encode()).hexdigest()},
                       "done": []})
    handler = _handler_203(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.reports == [{"request_id": REQUEST_ID, "status": "uploading",
                                "sizeBytes": len(content), "emergencyKey": key}]
    assert backend.put_ranges[0] == "bytes 0-999/2500"
    assert bytes(backend.received[REQUEST_ID]) == content
    assert len(_kit_notices()) == 1
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
async def test_restart_with_changed_key_fails_with_emergency_kit_changed(_clear_notices):
    hass, backend = FakeHass(), FakeBackend()
    content = _content(2500)
    manager = FakeManager(content=content)
    manager.agent.backups["bk7"] = FakeBackup("bk7", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "creating"
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk7",
                               "size": len(content), "chunk_bytes": CHUNK,
                               "settings": backup_module._parse_settings(KIT),
                               "key_sha256": hashlib.sha256(b"alter-schluessel").hexdigest()},
                       "done": []})
    handler = _handler_203(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.reports == [{"request_id": REQUEST_ID, "status": "failed", "error": "emergency_kit_changed"}]
    assert backend.put_ranges == []
    assert _kit_notices() == []
    assert "bk7" in manager.agent.backups


def test_kit_notice_has_texts_for_all_supported_languages():
    from ha_fleet_agent import backup_kit_notice
    from ha_fleet_agent.const import SUPPORTED_LANGUAGES

    for lang in SUPPORTED_LANGUAGES:
        texts = backup_kit_notice.texts(lang)
        assert texts["title"]
        assert "{date}" in texts["message"]
    assert backup_kit_notice.texts("xx") == backup_kit_notice.texts("en")


# ============================================================ Fehler- und Aufräumpfade (#211)


@pytest.mark.asyncio
async def test_unexpected_error_during_upload_reports_unexpected_error_and_keeps_encrypted_backup(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    async def _kaputt(*_args, **_kwargs):
        raise ValueError("unerwartet")

    monkeypatch.setattr(handler, "_upload", _kaputt)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "uploading", "failed"]
    assert backend.reports[-1]["error"] == "unexpected_error: ValueError: unerwartet"
    assert handler._find_done(REQUEST_ID) == {"request_id": REQUEST_ID, "status": "failed",
                                              "error": "unexpected_error"}
    assert handler._store.data["job"] is None
    assert manager.deleted == []
    assert "bk1" in manager.agent.backups


@pytest.mark.asyncio
async def test_unexpected_error_before_encryption_check_deletes_unencrypted_backup(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)
    klartext = FakeBackup("bk1", 0, False, {"fleet_agent.request_id": REQUEST_ID}, b"")
    klartext.size = "kaputt"  # int() scheitert, bevor „protected“ geprüft ist
    manager.agent.backups["bk1"] = klartext

    async def _create(*_args, **_kwargs):
        return klartext

    monkeypatch.setattr(handler, "_create", _create)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "failed"]
    assert backend.reports[-1]["error"].startswith("unexpected_error: ValueError")
    assert manager.deleted == [("bk1", ["hassio.local"])]


@pytest.mark.asyncio
async def test_unexpected_error_after_restart_reports_and_keeps_backup(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()
    content = _content()
    manager = FakeManager(content=content)
    manager.agent.backups["bk7"] = FakeBackup("bk7", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk7",
                               "size": len(content)}, "done": []})
    handler = _handler(hass, backend, manager, store=store)

    async def _kaputt(*_args, **_kwargs):
        raise KeyError("bk7")

    monkeypatch.setattr(handler, "_local_backup", _kaputt)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert backend.reports == [{"request_id": REQUEST_ID, "status": "failed",
                                "error": "unexpected_error: KeyError: 'bk7'"}]
    assert handler._find_done(REQUEST_ID)["error"] == "unexpected_error"
    assert store.data["job"] is None
    assert "bk7" in manager.agent.backups


@pytest.mark.asyncio
async def test_proxy_404_without_error_is_retried_like_a_server_error():
    hass, backend = FakeHass(), FakeBackend()
    content = _content()
    # Proxy-Fehlerseite während eines Deploys: 404 ohne JSON-``error``.
    backend.scripted.append(("PUT", "/content", FakeResponse(404)))
    manager = FakeManager(content=content)
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert bytes(backend.received[REQUEST_ID]) == content
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
async def test_backend_404_with_error_stays_final():
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("PUT", "/content", FakeResponse(404, {"error": "Resource not found"})))
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert len(backend.put_ranges) == 1
    assert backend.reports[-1] == {"request_id": REQUEST_ID, "status": "failed",
                                   "error": "upload_failed: HTTP 404 Resource not found"}


@pytest.mark.asyncio
async def test_proxy_404_on_report_is_retried():
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("POST", "/report", FakeResponse(404)))
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "uploading"]
    assert handler._find_done(REQUEST_ID)["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "resumed"), [({}, True), ({"error": "Resource not found"}, False)])
async def test_server_state_404_is_closed_only_with_backend_error(body, resumed):
    hass, backend = FakeHass(), FakeBackend()
    content = _content()
    manager = FakeManager(content=content)
    manager.agent.backups["bk7"] = FakeBackup("bk7", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "uploading"
    backend.scripted.append(("GET", "/content", FakeResponse(404, body)))
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk7",
                               "size": len(content), "chunk_bytes": CHUNK}, "done": []})
    handler = _handler(hass, backend, manager, store=store)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert (handler._find_done(REQUEST_ID)["status"] == "ready") is resumed
    assert bool(backend.put_ranges) is resumed


@pytest.mark.asyncio
async def test_unreachable_backend_books_failed_and_redelivery_reports_it():
    hass, backend = FakeHass(), FakeBackend()
    # Die Meldung „creating“ kommt in keinem der drei Versuche an.
    backend.scripted.extend([("POST", "/report", FakeResponse(503))] * 3)
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert backend.reports == []
    assert manager.initiate_calls == []
    assert handler._find_done(REQUEST_ID)["status"] == "failed"

    await _deliver(handler, hass)  # das Backend stellt den Auftrag erneut zu

    assert backend.reports == [{"request_id": REQUEST_ID, "status": "failed",
                                "error": "upload_failed: backend_unreachable"}]
    assert manager.initiate_calls == []


@pytest.mark.asyncio
async def test_closed_session_counts_as_network_error():
    hass, backend = FakeHass(), FakeBackend()

    def _closed(*_args, **_kwargs):
        raise RuntimeError("Session is closed")

    backend.post = _closed
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert handler._find_done(REQUEST_ID)["error"] == "upload_failed: backend_unreachable"


def _resume_handler(hass: FakeHass, backend: FakeBackend) -> tuple[BackupRequestHandler, FakeManager]:
    """Handler mit einem Auftrag im Upload, wie nach einem HA-Neustart."""
    content = _content()
    manager = FakeManager(content=content)
    manager.agent.backups["bk7"] = FakeBackup("bk7", len(content), True,
                                              {"fleet_agent.request_id": REQUEST_ID}, content)
    backend.status[REQUEST_ID] = "uploading"
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "uploading", "backup_id": "bk7",
                               "size": len(content), "chunk_bytes": CHUNK}, "done": []})
    return _handler(hass, backend, manager, store=store), manager


@pytest.mark.asyncio
async def test_server_state_404_with_backend_error_books_closed_without_report():
    hass, backend = FakeHass(), FakeBackend()
    backend.scripted.append(("GET", "/content", FakeResponse(404, {"error": "Resource not found"})))
    handler, manager = _resume_handler(hass, backend)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    assert handler._find_done(REQUEST_ID)["status"] == "closed"
    assert backend.reports == []
    assert "bk7" in manager.agent.backups


@pytest.mark.asyncio
async def test_persistent_proxy_404_after_restart_books_backend_unreachable():
    hass, backend = FakeHass(), FakeBackend()
    # Der Proxy antwortet in allen Versuchen mit 404 ohne ``error`` — das Backend ist nicht da.
    backend.scripted.extend([("GET", "/content", FakeResponse(404))] * backup_module.BACKUP_RESUME_ATTEMPTS)
    handler, manager = _resume_handler(hass, backend)

    await handler.async_setup()
    await handler._async_resume_after_start(hass)
    await hass.settle()

    done = handler._find_done(REQUEST_ID)
    assert done["status"] == "failed"
    assert done["error"] == "upload_failed: backend_unreachable"
    assert backend.put_ranges == []
    assert "bk7" in manager.agent.backups


@pytest.mark.asyncio
async def test_unreachable_complete_books_backend_unreachable():
    hass, backend = FakeHass(), FakeBackend()
    # Der Abschluss kommt in keinem der drei Versuche an.
    backend.scripted.extend([("POST", "/complete", FakeResponse(503))] * 3)
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert _statuses(backend) == ["creating", "uploading"]
    assert backend.completes == []
    done = handler._find_done(REQUEST_ID)
    assert done["status"] == "failed"
    assert done["error"] == "upload_failed: backend_unreachable"


class _HangingResponse(FakeResponse):
    async def __aenter__(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_shutdown_cancels_rereport_and_unsubscribes_start(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()
    unsubscribed: list[bool] = []
    monkeypatch.setattr(backup_module, "async_at_started",
                        lambda _hass, _func: lambda: unsubscribed.append(True))
    store = FakeStore({"job": None, "done": [{"request_id": REQUEST_ID, "status": "failed",
                                              "error": "create_failed"}]})
    handler = _handler(hass, backend, FakeManager(), store=store)
    await handler.async_setup()
    backend.scripted.append(("POST", "/report", _HangingResponse(204)))

    await handler.handle({"action": "backup_create", "requestId": REQUEST_ID})
    await asyncio.sleep(0)
    rereport = next(iter(handler._side_tasks))

    await handler.async_shutdown()

    assert rereport.cancelled()
    assert handler._side_tasks == set()
    assert unsubscribed == [True]


@pytest.mark.asyncio
async def test_shutdown_cancels_resume_routine_that_is_already_running(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()

    def _at_started(hass_, func):  # noqa: ANN001 — HA läuft schon: sofort aufrufen
        func(hass_)
        return lambda: None

    monkeypatch.setattr(backup_module, "async_at_started", _at_started)
    # Mitten im Erzeugen neu gestartet: Die Routine meldet ``ha_restarted`` und hängt dabei.
    backend.scripted.append(("POST", "/report", _HangingResponse(204)))
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "creating"}, "done": []})
    handler = _handler(hass, backend, FakeManager(content=_content()), store=store)

    await handler.async_setup()
    await asyncio.sleep(0)
    resume = next(iter(handler._side_tasks))

    await handler.async_shutdown()

    assert resume.cancelled()
    assert handler._find_done(REQUEST_ID) is None
    assert store.data["job"]["request_id"] == REQUEST_ID


@pytest.mark.asyncio
async def test_new_request_during_resume_routine_supersedes_old_job(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()
    new_id = "7f1c2d3e-0000-4000-8000-000000000002"
    handler, _manager = _resume_handler(hass, backend)
    store = handler._store
    await handler.async_setup()
    original_prune = handler._prune_local
    calls: list[int] = []

    async def _prune_mit_neuem_auftrag(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            return await original_prune(*args, **kwargs)
        # Während des Aufräumens stellt das Backend einen neuen Auftrag zu. Wie im Betrieb
        # (I/O in ``_prune_local``) speichert der neue Auftrag seinen Zustand, bevor die
        # Routine weitermacht.
        await handler.handle({"action": "backup_create", "requestId": new_id,
                              "maxBytes": 10_000, "chunkBytes": CHUNK})
        while not (store.data.get("job") or {}).get("request_id") == new_id:
            await asyncio.sleep(0)

    monkeypatch.setattr(handler, "_prune_local", _prune_mit_neuem_auftrag)

    await handler._async_resume_after_start(hass)

    assert handler._task_request_id == new_id
    assert handler._find_done(REQUEST_ID)["status"] == "closed"
    assert store.data["job"]["request_id"] == new_id  # der neue Auftrag bleibt gespeichert
    await hass.settle()
    assert handler._find_done(new_id)["status"] == "ready"
    assert REQUEST_ID not in backend.received  # der alte Auftrag lädt nichts mehr hoch


@pytest.mark.asyncio
async def test_same_request_redelivered_during_resume_routine_is_not_closed(monkeypatch):
    hass, backend = FakeHass(), FakeBackend()
    handler, _manager = _resume_handler(hass, backend)
    await handler.async_setup()

    async def _prune_mit_erneuter_zustellung(*_args, **_kwargs):
        await handler.handle({"action": "backup_create", "requestId": REQUEST_ID,
                              "maxBytes": 10_000, "chunkBytes": CHUNK})

    monkeypatch.setattr(handler, "_prune_local", _prune_mit_erneuter_zustellung)

    await handler._async_resume_after_start(hass)

    assert handler._task_request_id == REQUEST_ID
    assert handler._find_done(REQUEST_ID) is None


@pytest.mark.asyncio
async def test_failing_resume_routine_is_logged_not_lost(monkeypatch, caplog):
    hass, backend = FakeHass(), FakeBackend()

    def _at_started(hass_, func):  # noqa: ANN001 — HA läuft schon: sofort aufrufen
        func(hass_)
        return lambda: None

    def _kaputt(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(backup_module, "async_at_started", _at_started)
    backend.post = _kaputt
    store = FakeStore({"job": {"request_id": REQUEST_ID, "phase": "creating"}, "done": []})
    handler = _handler(hass, backend, FakeManager(content=_content()), store=store)

    await handler.async_setup()
    resume = next(iter(handler._side_tasks))
    await asyncio.gather(resume, return_exceptions=True)

    assert resume.exception() is None
    assert "Fortsetzen nach dem HA-Start gescheitert" in caplog.text


@pytest.mark.asyncio
async def test_other_runtime_error_is_unexpected_not_network_error():
    hass, backend = FakeHass(), FakeBackend()

    def _kaputt(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    backend.put = _kaputt
    manager = FakeManager(content=_content())
    handler = _handler(hass, backend, manager)

    await _deliver(handler, hass)

    assert backend.reports[-1] == {"request_id": REQUEST_ID, "status": "failed",
                                   "error": "unexpected_error: RuntimeError: kaputt"}
    assert handler._find_done(REQUEST_ID)["error"] == "unexpected_error"


@pytest.mark.asyncio
async def test_remove_store_deletes_the_backup_job_store(monkeypatch):
    removed: list[str] = []

    class _Store:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_remove(self):
            removed.append(self.key)

    monkeypatch.setattr(backup_module, "Store", _Store)

    await backup_module.async_remove_store(FakeHass(), "entry1")

    assert removed == ["ha_fleet_agent.backup_jobs.entry1"]
