"""BackupRequestHandler — Backup auf Knopfdruck (#168, Einstellungen #203).

Der Integrator drückt in Fleet Manager „Backup jetzt erstellen“. Der RequestPoller
liefert daraufhin die Aktion ``backup_create`` mit ``requestId``, ``maxBytes``,
``chunkBytes`` und ``settings`` (ab Backend #203). Dieser Handler

1. prüft die Voraussetzungen (HA ab 2025.8, Backup-Manager frei, Schlüssel gesetzt,
   lokale Backups verschlüsselt, Einstellungen erfüllbar) und meldet ``creating`` mit
   ``settingsApplied``,
2. erzeugt das Backup über den Backup-Manager von HA — mit dem Schlüssel aus dem
   HA-Notfallkit und dem Umfang laut ``settings``: wie die automatischen Backups der
   Instanz (Vorgabe) oder die eigene Auswahl aus Fleet Manager —, wartet auf dessen Ende
   und findet es über ``extra_metadata`` wieder,
3. meldet ``uploading`` mit der Größe (das Backend reserviert Platz oder lehnt ab), bei
   angefordertem Notfallkit samt Backup-Schlüssel, und zeigt dann einen Hinweis in HA,
4. lädt die Datei in Stücken hoch (``PUT …/content`` mit ``Content-Range``), setzt nach
   einem Abbruch am Stand des Servers fort und schließt mit dem SHA-256 ab,
5. lässt das Backup in HA liegen (#201) — der Kunde behält eine Kopie auf dem eigenen
   System, bei Fleet Manager liegt es nur vorübergehend. Manuelle Backups fallen nicht
   unter die Aufbewahrungsregel von HA; deshalb räumt der Handler selbst auf und behält
   nur die neuesten ``BACKUP_KEEP_LOCAL`` Backups mit seiner Markierung. Das gilt auch
   nach Abbruch oder Fehlschlag. Gelöscht wird sofort nur ein unverschlüsseltes Backup.

**Nur verschlüsselt (E3).** Schreibt HA Klartext — kein Schlüssel eingerichtet oder der
lokale Speicherort steht auf „unverschlüsselt“ —, bricht der Handler mit klarer Meldung ab,
statt Zugangsdaten unverschlüsselt aus dem Haus zu geben. Das Backend prüft die Datei beim
Abschluss noch einmal.

**Bewusst ``async_initiate_backup`` statt ``async_create_backup``.** Letzteres wartet bis
zum Ende; wird der wartende Task abgebrochen, bricht auch der interne Abschluss von HA ab.
Auf das Ende wartet der Handler deshalb über die Ereignisse des Managers.

**Neustarts und Doppelzustellung.** Der Auftragszustand liegt im ``Store``
``ha_fleet_agent.backup_jobs.<entry_id>``. Nach einem HA-Neustart setzt ein Upload fort,
ein Auftrag mitten im Erzeugen endet mit ``ha_restarted``, und von den Backups mit
unserer Markierung bleiben die neuesten ``BACKUP_KEEP_LOCAL``. Stellt das Backend eine bekannte
``requestId`` erneut zu, wird ein laufender Auftrag nicht doppelt gestartet und ein
erledigter meldet seinen Endzustand noch einmal.

**Einstellungen exakt (#203, A4).** Kann das Plugin eine eigene Auswahl nicht erfüllen —
Ordner oder Add-ons ohne Supervisor, ein gewähltes Add-on ist nicht installiert —, bricht es
mit Fehlercode ab und erzeugt nichts, statt stillschweigend etwas anderes zu sichern.

**Notfallkit (#203, S5/S6).** Den Schlüssel schickt das Plugin nur auf Anforderung mit und
schreibt ihn nie ins Log. Der Store hält nur seinen SHA-256; muss ``uploading`` nach einem
Neustart erneut gemeldet werden, liest das Plugin den Schlüssel neu aus HA und bricht mit
``emergency_kit_changed`` ab, wenn er sich geändert hat. Den Backup-Vorgang zeigt HA selbst
(E5); dass der Schlüssel an Fleet Manager ging, meldet ``backup_kit_notice``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from . import backup_kit_notice
from .const import (
    BACKUP_CHUNK_ATTEMPTS,
    BACKUP_CHUNK_BACKOFF_SECONDS,
    BACKUP_CHUNK_BYTES,
    BACKUP_CHUNK_TIMEOUT_SECONDS,
    BACKUP_COMPLETE_TIMEOUT_SECONDS,
    BACKUP_CREATE_TIMEOUT_SECONDS,
    BACKUP_DEFAULT_RETRY_AFTER_SECONDS,
    BACKUP_DONE_HISTORY,
    BACKUP_FOLDERS,
    BACKUP_KEEP_LOCAL,
    BACKUP_LOCAL_AGENT_IDS,
    BACKUP_MAX_BYTES,
    BACKUP_MAX_SLOT_WAITS,
    BACKUP_METADATA_KEY,
    BACKUP_MIN_HA_VERSION,
    BACKUP_NAME_SUFFIX,
    BACKUP_READ_PIECE_BYTES,
    BACKUP_REPORT_BACKOFF_SECONDS,
    BACKUP_REPORT_TIMEOUT_SECONDS,
    BACKUP_RESUME_ATTEMPTS,
    BACKUP_RESUME_RETRY_SECONDS,
    BACKUP_STORAGE_KEY_PREFIX,
    DEFAULT_LANGUAGE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Meldungen an das Backend.
REPORT_CREATING = "creating"
REPORT_UPLOADING = "uploading"
REPORT_FAILED = "failed"

# Fehlercodes, die das Plugin meldet. Das Frontend übersetzt sie; ein Code mit
# „: Text“ dahinter trägt die Meldung von HA für die Fehlersuche.
ERROR_HA_VERSION_UNSUPPORTED = "ha_version_unsupported"
ERROR_HA_BUSY = "ha_busy"
ERROR_ENCRYPTION_KEY_MISSING = "encryption_key_missing"
ERROR_LOCAL_BACKUPS_UNENCRYPTED = "local_backups_unencrypted"
ERROR_LOCAL_AGENT_MISSING = "local_agent_missing"
ERROR_BACKUP_NOT_ENCRYPTED = "backup_not_encrypted"
ERROR_BACKUP_TOO_LARGE = "backup_too_large"
ERROR_BACKUP_NOT_FOUND = "backup_not_found"
ERROR_CREATE_FAILED = "create_failed"
ERROR_CREATE_TIMEOUT = "create_timeout"
ERROR_HA_RESTARTED = "ha_restarted"
ERROR_READ_FAILED = "read_failed"
ERROR_UPLOAD_FAILED = "upload_failed"
ERROR_SETTINGS_UNSUPPORTED = "settings_unsupported"
ERROR_ADDON_NOT_INSTALLED = "addon_not_installed"
ERROR_EMERGENCY_KIT_CHANGED = "emergency_kit_changed"

# Modus der Backup-Einstellungen (#203). Ohne ``settings`` im Poll gilt „automatic“.
MODE_AUTOMATIC = "automatic"
MODE_CUSTOM = "custom"

# Phasen im gespeicherten Auftragszustand.
PHASE_CREATING = "creating"
PHASE_UPLOADING = "uploading"

# Endzustände in der Merkliste erledigter Aufträge.
DONE_READY = "ready"
DONE_FAILED = "failed"
DONE_CLOSED = "closed"

# Fehlermeldungen von HA werden für den Report gekürzt.
MAX_DETAIL_LEN = 300


class BackupJobFailed(Exception):
    """Der Auftrag scheitert mit einem Fehlercode, den das Plugin ans Backend meldet."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class BackupJobAborted(Exception):
    """Der Auftrag endet ohne eigenen Fehlerreport.

    Das Backend hat ihn geschlossen (``410``) oder selbst als fehlgeschlagen gebucht
    (Kontingent, Prüfung beim Abschluss) — oder ist gar nicht erreichbar.
    """


class _OffsetMismatch(Exception):
    """Der Server hat einen anderen Stand als erwartet; Upload ab ``received`` neu aufsetzen."""

    def __init__(self, received: int) -> None:
        super().__init__(received)
        self.received = received


def _default_manager_getter(hass: HomeAssistant) -> Any:
    """Holt den Backup-Manager von HA. Import erst hier, damit ältere HA-Versionen das
    Plugin trotzdem laden — sie scheitern dann nur an dieser Funktion."""
    from homeassistant.components.backup import async_get_manager  # noqa: PLC0415

    return async_get_manager(hass)


def _default_addons_getter(hass: HomeAssistant) -> set[str] | None:
    """Slugs der installierten Add-ons aus der Supervisor-Info (Quelle wie
    ``StateReporter._list_addons``). ``None``, wenn es keinen Supervisor gibt oder die
    Info nicht lesbar ist — dann prüft HA selbst beim Erzeugen."""
    for module_path in ("homeassistant.components.hassio", "homeassistant.helpers.hassio"):
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue
        get_info = getattr(module, "get_supervisor_info", None)
        if get_info is None:
            continue
        try:
            info = get_info(hass)
        except Exception:  # noqa: BLE001 — ohne Info keine Vorabprüfung
            return None
        if not isinstance(info, dict):
            return None
        return {
            str(addon["slug"]) for addon in info.get("addons") or []
            if isinstance(addon, dict) and addon.get("slug")
        }
    return None


def _to_folders(values: list[str]) -> list[Any]:
    """Ordnernamen in das HA-Enum ``Folder`` umwandeln; ohne Import bleiben die Strings
    (``Folder`` ist ein ``StrEnum``, die Werte sind dieselben)."""
    try:
        from homeassistant.components.backup import Folder  # noqa: PLC0415
    except ImportError:
        return list(values)
    return [Folder(v) for v in values]


def _running_ha_version() -> tuple[int, int] | None:
    """Laufende HA-Version als ``(major, minor)`` oder ``None``, wenn nicht lesbar."""
    try:
        from homeassistant import const as ha_const  # noqa: PLC0415

        major = int(getattr(ha_const, "MAJOR_VERSION"))
        minor = int(getattr(ha_const, "MINOR_VERSION"))
        return (major, minor)
    except (AttributeError, TypeError, ValueError, ImportError):
        return None


class BackupRequestHandler:
    """Verarbeitet die Poll-Aktion ``backup_create`` (#168)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        session: aiohttp.ClientSession,
        backend_url: str,
        api_key: str,
        *,
        manager_getter: Callable[[HomeAssistant], Any] | None = None,
        ha_version: tuple[int, int] | None = None,
        store: Any | None = None,
        addons_getter: Callable[[HomeAssistant], set[str] | None] | None = None,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._language = language
        self._addons_getter = addons_getter or _default_addons_getter
        self._session = session
        self._backend_url = backend_url.rstrip("/")
        self._api_key = api_key
        self._manager_getter = manager_getter or _default_manager_getter
        self._ha_version = ha_version if ha_version is not None else _running_ha_version()
        self._store = store or Store(
            hass, STORAGE_VERSION, f"{BACKUP_STORAGE_KEY_PREFIX}.{entry_id}"
        )
        # Gespeicherter Auftrag: {request_id, phase, backup_id?, size?, chunk_bytes, max_bytes,
        # settings, key_sha256?}. Den Schlüssel selbst speichert er nie.
        self._job: dict[str, Any] | None = None
        # Erledigte Aufträge, jüngster zuletzt: {request_id, status, error?, sha256?, size?}.
        self._done: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._task_request_id: str | None = None
        # Aufträge, die ein neuer abgelöst hat: Ihr Abbruch bucht sie als geschlossen —
        # beim Herunterfahren von HA nicht, damit der Upload danach fortsetzen kann. Je
        # Auftrag statt als ein Flag, weil der neue Task schon läuft, wenn der alte seinen
        # Abbruch verarbeitet.
        self._superseded: set[str] = set()

    # ------------------------------------------------------------------ Lebenszyklus

    async def async_setup(self) -> None:
        """Lädt den Auftragszustand und setzt nach dem Start von HA fort."""
        data = await self._store.async_load() or {}
        job = data.get("job")
        self._job = job if isinstance(job, dict) and job.get("request_id") else None
        done = data.get("done")
        self._done = [d for d in done if isinstance(d, dict)][-BACKUP_DONE_HISTORY:] if isinstance(done, list) else []
        # Der Backup-Manager ist bis zum Ende des Starts blockiert — erst danach aufräumen
        # oder fortsetzen. Läuft HA schon, ruft HA die Routine sofort auf.
        async_at_started(self._hass, self._async_resume_after_start)

    async def async_shutdown(self) -> None:
        """Beim Entladen: laufenden Auftrag stoppen, Zustand bleibt für die Fortsetzung."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — Entladen darf nicht scheitern
                _LOGGER.debug("Backup-Auftrag beim Entladen mit Fehler beendet", exc_info=True)

    # ------------------------------------------------------------------ Poll-Aktion

    async def handle(self, data: dict[str, Any]) -> None:
        """Poll-Handler für ``action == "backup_create"``. Kehrt sofort zurück."""
        request_id = str(data.get("requestId") or data.get("request_id") or "")
        if not request_id:
            _LOGGER.warning("backup_create ohne requestId — ignoriert")
            return
        max_bytes = _positive_int(data.get("maxBytes"), BACKUP_MAX_BYTES)
        chunk_bytes = _positive_int(data.get("chunkBytes"), BACKUP_CHUNK_BYTES)
        settings = _parse_settings(data.get("settings"))

        if self._is_running(request_id):
            _LOGGER.debug("backup_create %s läuft bereits — erneute Zustellung ignoriert", request_id)
            return
        done = self._find_done(request_id)
        if done is not None:
            _LOGGER.info("backup_create %s ist schon erledigt (%s) — Endzustand erneut melden",
                         request_id, done.get("status"))
            self._start(self._rereport(done), request_id=None)
            return

        if self._task is not None and not self._task.done():
            # Das Backend hält je Installation nur einen offenen Auftrag. Kommt ein neuer,
            # ist der alte dort beendet (abgebrochen oder abgelaufen).
            _LOGGER.info("Neuer Backup-Auftrag %s — bisheriger %s wird beendet",
                         request_id, self._task_request_id)
            if self._task_request_id is not None:
                self._superseded.add(self._task_request_id)
            self._task.cancel()

        _LOGGER.info("Backup auf Knopfdruck angefordert: %s (Inhalt %s, Notfallkit %s)", request_id,
                     settings["mode"], "ja" if settings["include_emergency_kit"] else "nein")
        self._start(self._run_new(request_id, max_bytes, chunk_bytes, settings), request_id=request_id)

    def _start(self, coro: Any, *, request_id: str | None) -> None:
        create = getattr(self._hass, "async_create_background_task", None)
        name = f"hafm_backup_{request_id or 'report'}"
        if create is not None:
            task = create(coro, name)
        else:  # ältere HA-Versionen
            task = self._hass.async_create_task(coro, name=name)
        if request_id is not None:
            self._task = task
            self._task_request_id = request_id

    def _is_running(self, request_id: str) -> bool:
        return (
            self._task_request_id == request_id
            and self._task is not None
            and not self._task.done()
        )

    def _find_done(self, request_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._done):
            if entry.get("request_id") == request_id:
                return entry
        return None

    # ------------------------------------------------------------------ Ablauf

    async def _run_new(self, request_id: str, max_bytes: int, chunk_bytes: int,
                       settings: dict[str, Any]) -> None:
        """Ein neuer Auftrag vom Knopf bis zum Abschluss beim Server."""
        manager: Any = None
        local_id: str | None = None
        backup_id: str | None = None
        try:
            await self._save_job({
                "request_id": request_id,
                "phase": PHASE_CREATING,
                "max_bytes": max_bytes,
                "chunk_bytes": chunk_bytes,
                "settings": settings,
            })
            manager, local_id = self._precheck()
            self._check_settings(local_id, settings)
            await self._report(request_id, REPORT_CREATING, settings_applied=True)

            # Genau dieser Schlüssel verschlüsselt das Backup — und geht als Notfallkit mit.
            password = manager.config.data.create_backup.password
            backup = await self._create(manager, local_id, request_id, settings, password)
            backup_id = backup.backup_id
            size = int(backup.size)
            # Vor dem Speichern der Phase „uploading“ prüfen: Ein gespeicherter Auftrag in
            # dieser Phase trägt so immer ein verschlüsseltes Backup — auch wenn HA genau
            # hier herunterfährt und der Neustart-Pfad das Backup als laufendes behält.
            if not getattr(backup, "protected", False):
                raise BackupJobFailed(ERROR_BACKUP_NOT_ENCRYPTED)
            job: dict[str, Any] = {
                "request_id": request_id,
                "phase": PHASE_UPLOADING,
                "backup_id": backup_id,
                "size": size,
                "max_bytes": max_bytes,
                "chunk_bytes": chunk_bytes,
                "settings": settings,
            }
            if settings["include_emergency_kit"]:
                job["key_sha256"] = _key_hash(password)
            await self._save_job(job)
            # Erst jetzt aufräumen: Scheitert das Erzeugen, bleibt der bisherige Bestand.
            # Das neue Backup bleibt ab hier in HA — auch wenn die Übertragung scheitert.
            await self._prune_local(manager, local_id, keep_backup_id=backup_id)
            if size > max_bytes:
                raise BackupJobFailed(ERROR_BACKUP_TOO_LARGE, f"{size} > {max_bytes}")

            await self._report_uploading(manager, request_id, job, password)
            sha256 = await self._upload(manager, local_id, request_id, backup_id, size, chunk_bytes, 0)
            await self._complete(request_id, sha256, size)
            _LOGGER.info("Backup-Auftrag %s fertig übertragen (%d Byte)", request_id, size)
            await self._finish(request_id, DONE_READY, sha256=sha256, size=size)
        except BackupJobFailed as err:
            _LOGGER.warning("Backup-Auftrag %s fehlgeschlagen: %s", request_id, err)
            await self._report_failed(request_id, err)
            await self._finish(request_id, DONE_FAILED, error=err.code)
            if err.code == ERROR_BACKUP_NOT_ENCRYPTED:
                # Klartext bleibt nicht liegen (E3) — das einzige Backup, das sofort geht.
                await self._delete_local(manager, local_id, backup_id)
        except BackupJobAborted as err:
            _LOGGER.info("Backup-Auftrag %s beendet: %s", request_id, err)
            await self._finish(request_id, DONE_CLOSED)
        except asyncio.CancelledError:
            if request_id in self._superseded:
                # Ein neuer Auftrag hat diesen abgelöst — der alte ist im Backend beendet.
                self._superseded.discard(request_id)
                await self._finish(request_id, DONE_CLOSED)
            # Sonst fährt HA herunter: Zustand bleibt, der Upload setzt danach fort.
            raise

    def _precheck(self) -> tuple[Any, str]:
        """Voraussetzungen prüfen, bevor irgendetwas angelegt wird."""
        if self._ha_version is not None and self._ha_version < BACKUP_MIN_HA_VERSION:
            raise BackupJobFailed(
                ERROR_HA_VERSION_UNSUPPORTED, f"{self._ha_version[0]}.{self._ha_version[1]}"
            )
        try:
            manager = self._manager_getter(self._hass)
        except Exception as err:  # noqa: BLE001 — ImportError, HomeAssistantError …
            raise BackupJobFailed(ERROR_HA_VERSION_UNSUPPORTED, str(err)) from err

        state = str(getattr(manager, "state", ""))
        if state != "idle":
            # Anderes Backup, Update mit Backup, Wiederherstellung — oder HA startet noch.
            raise BackupJobFailed(ERROR_HA_BUSY, state)

        agents = getattr(manager, "backup_agents", {}) or {}
        local_id = next((a for a in BACKUP_LOCAL_AGENT_IDS if a in agents), None)
        if local_id is None:
            raise BackupJobFailed(ERROR_LOCAL_AGENT_MISSING)

        config = manager.config.data
        if not config.create_backup.password:
            raise BackupJobFailed(ERROR_ENCRYPTION_KEY_MISSING)
        agent_config = (config.agents or {}).get(local_id)
        if agent_config is not None and not agent_config.protected:
            # HA setzt das Passwort für diesen Ort auf None und schreibt Klartext.
            raise BackupJobFailed(ERROR_LOCAL_BACKUPS_UNENCRYPTED)
        return manager, local_id

    def _check_settings(self, local_id: str, settings: dict[str, Any]) -> None:
        """Ist die eigene Auswahl hier erfüllbar (#203, A4)? Sonst Abbruch vor dem Erzeugen."""
        mode = settings["mode"]
        if mode == MODE_AUTOMATIC:
            return
        if mode != MODE_CUSTOM:
            raise BackupJobFailed(ERROR_SETTINGS_UNSUPPORTED, f"mode {mode}")
        folders = settings["include_folders"]
        addons = settings["include_addons"]
        unknown = [f for f in folders if f not in BACKUP_FOLDERS]
        if unknown:
            raise BackupJobFailed(ERROR_SETTINGS_UNSUPPORTED, ",".join(unknown))
        if settings["include_all_addons"] and addons:
            # HA: „Cannot include all addons and specify specific addons“.
            raise BackupJobFailed(ERROR_SETTINGS_UNSUPPORTED, "all_and_single_addons")
        if local_id != "hassio.local" and (folders or addons or settings["include_all_addons"]):
            # HA: „Addons and folders are not supported by core backup“.
            raise BackupJobFailed(ERROR_SETTINGS_UNSUPPORTED, "no_supervisor")
        if addons:
            installed = self._addons_getter(self._hass)
            if installed is not None:
                missing = [slug for slug in addons if slug not in installed]
                if missing:
                    raise BackupJobFailed(ERROR_ADDON_NOT_INSTALLED, ",".join(missing))

    async def _create(self, manager: Any, local_id: str, request_id: str,
                      settings: dict[str, Any], password: str | None) -> Any:
        """Erzeugt das Backup und wartet auf sein Ende; liefert das lokale ``AgentBackup``."""
        cfg = manager.config.data.create_backup
        supervised = local_id == "hassio.local"
        if settings["mode"] == MODE_CUSTOM:
            # Eigene Auswahl aus Fleet Manager (#203); erfüllbar laut _check_settings.
            include_all_addons = supervised and bool(settings["include_all_addons"])
            include_addons = (
                list(settings["include_addons"])
                if supervised and settings["include_addons"] and not include_all_addons else None
            )
            include_folders = (
                _to_folders(settings["include_folders"]) if supervised and settings["include_folders"] else None
            )
            include_database = bool(settings["include_database"])
        else:
            # Wie die automatischen Backups; auf Core/Container erlaubt HA nur HA selbst und
            # die Datenbank (E9).
            include_all_addons = bool(cfg.include_all_addons) if supervised else False
            include_addons = (
                list(cfg.include_addons) if supervised and cfg.include_addons and not include_all_addons else None
            )
            include_folders = list(cfg.include_folders) if supervised and cfg.include_folders else None
            include_database = bool(cfg.include_database)

        loop = asyncio.get_running_loop()
        finished: asyncio.Future = loop.create_future()

        def _on_event(event: Any) -> None:
            state = getattr(event, "state", None)
            if (
                str(getattr(event, "manager_state", "")) == "create_backup"
                and str(state) in ("completed", "failed")
                and not finished.done()
            ):
                finished.set_result((str(state), getattr(event, "reason", None)))

        unsubscribe = manager.async_subscribe_events(_on_event)
        try:
            try:
                await manager.async_initiate_backup(
                    agent_ids=[local_id],
                    extra_metadata={BACKUP_METADATA_KEY: request_id},
                    include_addons=include_addons,
                    include_all_addons=include_all_addons,
                    include_database=include_database,
                    include_folders=include_folders,
                    # Die HA-Einstellungen sind immer dabei (A5).
                    include_homeassistant=True,
                    name=f"Fleet Manager {datetime.now():%Y-%m-%d %H:%M}{BACKUP_NAME_SUFFIX}",
                    password=password,
                )
            except Exception as err:  # noqa: BLE001 — BackupManagerError u. a.
                code = ERROR_HA_BUSY if "busy" in str(err).lower() else ERROR_CREATE_FAILED
                raise BackupJobFailed(code, _short(err)) from err
            try:
                state, reason = await asyncio.wait_for(finished, BACKUP_CREATE_TIMEOUT_SECONDS)
            except TimeoutError as err:
                raise BackupJobFailed(ERROR_CREATE_TIMEOUT) from err
        finally:
            unsubscribe()

        if state != "completed":
            raise BackupJobFailed(ERROR_CREATE_FAILED, reason)
        backup = await self._find_backup(manager, local_id, request_id)
        if backup is None:
            raise BackupJobFailed(ERROR_BACKUP_NOT_FOUND)
        return backup

    async def _find_backup(self, manager: Any, local_id: str, request_id: str) -> Any | None:
        """Sucht das Backup mit unserer Markierung — nur im lokalen Agent, nicht in der Cloud."""
        for backup in await manager.backup_agents[local_id].async_list_backups():
            if (getattr(backup, "extra_metadata", None) or {}).get(BACKUP_METADATA_KEY) == request_id:
                return backup
        return None

    # ------------------------------------------------------------------ Upload

    async def _upload(
        self,
        manager: Any,
        local_id: str,
        request_id: str,
        backup_id: str,
        size: int,
        chunk_bytes: int,
        offset: int,
    ) -> str:
        """Lädt ab ``offset`` hoch und liefert den SHA-256 der ganzen Datei.

        Liest die Datei immer von vorn — der Hash läuft über alles —, schickt aber erst ab
        dem Stand des Servers. Meldet der Server einen anderen Stand, beginnt es dort neu.
        """
        for _attempt in range(3):
            try:
                return await self._upload_from(manager, local_id, request_id, backup_id,
                                               size, chunk_bytes, offset)
            except _OffsetMismatch as mismatch:
                _LOGGER.info("Backup-Auftrag %s: Server steht bei %d Byte — setze dort fort",
                             request_id, mismatch.received)
                offset = mismatch.received
        raise BackupJobFailed(ERROR_UPLOAD_FAILED, "offset_mismatch")

    async def _upload_from(
        self,
        manager: Any,
        local_id: str,
        request_id: str,
        backup_id: str,
        size: int,
        chunk_bytes: int,
        offset: int,
    ) -> str:
        hasher = hashlib.sha256()
        position = 0
        # aclosing: Bricht der Upload ab, schließt die Quelle sofort (Datei, Supervisor-Stream).
        async with aclosing(self._chunks(manager, local_id, backup_id, chunk_bytes)) as chunks:
            async for chunk in chunks:
                hasher.update(chunk)
                start = position
                end = position + len(chunk)
                position = end
                if end > size:
                    raise BackupJobFailed(ERROR_READ_FAILED, f"Datei größer als {size} Byte")
                if end <= offset:
                    continue  # schon beim Server
                part = chunk[offset - start:] if start < offset else chunk
                await self._put_chunk(request_id, part, max(start, offset), size)
        if position != size:
            raise BackupJobFailed(ERROR_READ_FAILED, f"{position} statt {size} Byte gelesen")
        return hasher.hexdigest()

    async def _chunks(
        self, manager: Any, local_id: str, backup_id: str, chunk_bytes: int
    ) -> AsyncIterator[bytes]:
        """Liefert die Datei in Stücken zu ``chunk_bytes``."""
        try:
            source = await self._open_source(manager, local_id, backup_id)
        except BackupJobFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise BackupJobFailed(ERROR_READ_FAILED, _short(err)) from err
        buffer = bytearray()
        try:
            async for piece in source:
                buffer.extend(piece)
                while len(buffer) >= chunk_bytes:
                    yield bytes(buffer[:chunk_bytes])
                    del buffer[:chunk_bytes]
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — Lesefehler von Datei oder Supervisor
            raise BackupJobFailed(ERROR_READ_FAILED, _short(err)) from err
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
        if buffer:
            yield bytes(buffer)

    async def _open_source(self, manager: Any, local_id: str, backup_id: str) -> AsyncIterator[bytes]:
        """Core/Container: Datei direkt lesen (``async_download_backup`` wirft dort
        ``NotImplementedError``). HA OS: Byte-Stream über die Supervisor-API."""
        local_agents = getattr(manager, "local_backup_agents", None) or {}
        if local_id in local_agents:
            agent = local_agents[local_id]
            await agent.async_list_backups()  # lädt die Pfade, sonst kennt get_backup_path nichts
            return self._read_file(agent.get_backup_path(backup_id))
        return await manager.backup_agents[local_id].async_download_backup(backup_id)

    async def _read_file(self, path: Any) -> AsyncIterator[bytes]:
        handle = await self._hass.async_add_executor_job(open, path, "rb")
        try:
            while True:
                piece = await self._hass.async_add_executor_job(handle.read, BACKUP_READ_PIECE_BYTES)
                if not piece:
                    return
                yield piece
        finally:
            await self._hass.async_add_executor_job(handle.close)

    async def _put_chunk(self, request_id: str, data: bytes, start: int, total: int) -> None:
        """Schickt ein Stück; wiederholt bei Netzfehlern und wartet bei belegten Plätzen."""
        url = f"{self._backend_url}/api/agent/backup-requests/{request_id}/content"
        end = start + len(data)
        headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end - 1}/{total}",
        }
        timeout = aiohttp.ClientTimeout(total=BACKUP_CHUNK_TIMEOUT_SECONDS)
        failures = 0
        slot_waits = 0
        while True:
            retry_after: float | None = None
            last_error = ""
            try:
                async with self._session.put(url, data=data, headers=headers, timeout=timeout) as resp:
                    if 200 <= resp.status < 300:
                        return
                    body = await _json_body(resp)
                    error = str(body.get("error") or "")
                    if resp.status == 409 and error == "offset_mismatch":
                        received = _positive_int(body.get("receivedBytes"), 0)
                        if received == end:
                            return  # Stück kam an, nur die Antwort ging verloren
                        raise _OffsetMismatch(received)
                    if resp.status == 410:
                        raise BackupJobAborted("request_closed")
                    if resp.status == 503:
                        slot_waits += 1
                        if slot_waits > BACKUP_MAX_SLOT_WAITS:
                            raise BackupJobFailed(ERROR_UPLOAD_FAILED, "upload_slots_exhausted")
                        retry_after = _retry_after(resp)
                    elif resp.status >= 500 or resp.status in (408, 429, 499) or error == "upload_busy":
                        last_error = f"HTTP {resp.status} {error}".strip()
                    else:
                        raise BackupJobFailed(ERROR_UPLOAD_FAILED, f"HTTP {resp.status} {error}".strip())
            except (aiohttp.ClientError, TimeoutError) as err:
                last_error = _short(err) or type(err).__name__
            if retry_after is not None:
                await asyncio.sleep(retry_after)
                continue
            failures += 1
            if failures >= BACKUP_CHUNK_ATTEMPTS:
                raise BackupJobFailed(ERROR_UPLOAD_FAILED, last_error)
            await asyncio.sleep(BACKUP_CHUNK_BACKOFF_SECONDS[min(failures - 1, len(BACKUP_CHUNK_BACKOFF_SECONDS) - 1)])

    async def _complete(self, request_id: str, sha256: str, size: int) -> None:
        """Schließt ab. Das Backend prüft Größe, Hash, Format und Verschlüsselung."""
        url = f"{self._backend_url}/api/agent/backup-requests/{request_id}/complete"
        body = {"sha256": sha256, "sizeBytes": size}
        status, error = await self._post(url, body, BACKUP_COMPLETE_TIMEOUT_SECONDS)
        if status is None:
            raise BackupJobAborted(f"Abschluss nicht erreichbar: {error}")
        if status == 410:
            raise BackupJobAborted("request_closed")
        if status >= 300:
            # Das Backend hat den Auftrag selbst auf FAILED gebucht und die Datei gelöscht.
            raise BackupJobAborted(f"Abschluss abgelehnt: HTTP {status} {error}".strip())

    async def _server_state(self, request_id: str) -> dict[str, Any] | None:
        """``GET …/content``: Stand beim Server, ``{"status": "closed"}`` bei 410, sonst None."""
        url = f"{self._backend_url}/api/agent/backup-requests/{request_id}/content"
        timeout = aiohttp.ClientTimeout(total=BACKUP_REPORT_TIMEOUT_SECONDS)
        try:
            async with self._session.get(url, headers={"X-API-Key": self._api_key}, timeout=timeout) as resp:
                if resp.status == 410 or resp.status == 404:
                    return {"status": "closed"}
                if 200 <= resp.status < 300:
                    return await _json_body(resp)
                return None
        except (aiohttp.ClientError, TimeoutError):
            return None

    # ------------------------------------------------------------------ Meldungen

    async def _report(self, request_id: str, status: str, *, size_bytes: int | None = None,
                      error: str | None = None, settings_applied: bool | None = None,
                      emergency_key: str | None = None) -> None:
        """Meldet einen Zwischenstand. Lehnt das Backend ab, endet der Auftrag.

        Den Body loggt diese Methode nie — er kann den Backup-Schlüssel tragen (#203)."""
        body: dict[str, Any] = {"status": status}
        if size_bytes is not None:
            body["sizeBytes"] = size_bytes
        if error:
            body["error"] = error
        if settings_applied is not None:
            body["settingsApplied"] = settings_applied
        if emergency_key:
            body["emergencyKey"] = emergency_key
        url = f"{self._backend_url}/api/agent/backup-requests/{request_id}/report"
        code, detail = await self._post(url, body, BACKUP_REPORT_TIMEOUT_SECONDS)
        if code is None:
            raise BackupJobAborted(f"Backend nicht erreichbar: {detail}")
        if code >= 300:
            # 410 geschlossen, 409/413 Platz oder Größe abgelehnt — das Backend hat den
            # Auftrag dann schon selbst beendet.
            raise BackupJobAborted(f"Meldung „{status}“ abgelehnt: HTTP {code} {detail}".strip())

    async def _report_uploading(self, manager: Any, request_id: str, job: dict[str, Any],
                                password: str | None = None) -> None:
        """Meldet ``uploading`` mit Größe, bei Notfallkit samt Schlüssel, und zeigt danach
        den Hinweis in HA (S6).

        Ohne ``password`` (erneute Meldung nach einem Neustart) liest es den Schlüssel neu aus
        HA; weicht dessen Hash vom gespeicherten ab, verschlüsselt er dieses Backup nicht."""
        settings = job.get("settings") or {}
        key: str | None = None
        if settings.get("include_emergency_kit"):
            key = password if password is not None else manager.config.data.create_backup.password
            if not key or _key_hash(key) != job.get("key_sha256"):
                raise BackupJobFailed(ERROR_EMERGENCY_KIT_CHANGED)
        await self._report(request_id, REPORT_UPLOADING, size_bytes=int(job["size"]), emergency_key=key)
        if key:
            _LOGGER.info("Backup-Auftrag %s: Backup-Schlüssel als Notfallkit übermittelt", request_id)
            try:
                backup_kit_notice.async_show(self._hass, self._entry_id, self._language,
                                             f"{datetime.now():%Y-%m-%d %H:%M}")
            except Exception:  # noqa: BLE001 — der Hinweis darf den Upload nicht verhindern
                _LOGGER.warning("Hinweis zum Notfallkit nicht angelegt", exc_info=True)

    async def _report_failed(self, request_id: str, err: BackupJobFailed) -> None:
        """Meldet einen Fehlschlag — nach Kräften, ohne selbst zu scheitern."""
        error = err.code if not err.detail else f"{err.code}: {_one_line(err.detail)}"
        try:
            await self._report(request_id, REPORT_FAILED, error=error[:500])
        except BackupJobAborted as aborted:
            _LOGGER.debug("Fehlerreport für %s nicht angenommen: %s", request_id, aborted)

    async def _post(self, url: str, body: dict[str, Any], timeout_s: float) -> tuple[int | None, str]:
        """POST mit drei Versuchen bei Netzfehlern und 5xx. Liefert (Status, Fehlercode)."""
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        attempts = len(BACKUP_REPORT_BACKOFF_SECONDS) + 1
        last: tuple[int | None, str] = (None, "")
        for attempt in range(attempts):
            try:
                async with self._session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status < 500:
                        payload = await _json_body(resp) if resp.status >= 300 else {}
                        return resp.status, str(payload.get("error") or "")
                    last = (None, f"HTTP {resp.status}")
            except (aiohttp.ClientError, TimeoutError) as err:
                last = (None, _short(err) or type(err).__name__)
            if attempt < len(BACKUP_REPORT_BACKOFF_SECONDS):
                await asyncio.sleep(BACKUP_REPORT_BACKOFF_SECONDS[attempt])
        return last

    async def _rereport(self, done: dict[str, Any]) -> None:
        """Beantwortet eine erneute Zustellung eines erledigten Auftrags."""
        request_id = str(done.get("request_id"))
        status = done.get("status")
        try:
            if status == DONE_FAILED:
                await self._report(request_id, REPORT_FAILED, error=str(done.get("error") or "agent_error"))
            elif status == DONE_READY and done.get("sha256") and done.get("size") is not None:
                # Die Datei liegt vollständig beim Server; der Abschluss ist dort idempotent.
                await self._complete(request_id, str(done["sha256"]), int(done["size"]))
        except BackupJobAborted as err:
            _LOGGER.debug("Erneuter Endzustand für %s nicht angenommen: %s", request_id, err)

    # ------------------------------------------------------------------ Neustart

    async def _async_resume_after_start(self, _hass: HomeAssistant | None = None) -> None:
        """Nach dem Start von HA: Auftrag fortsetzen oder beenden, Reste aufräumen."""
        job = self._job
        try:
            manager = self._manager_getter(self._hass)
            local_id = next((a for a in BACKUP_LOCAL_AGENT_IDS if a in (manager.backup_agents or {})), None)
        except Exception:  # noqa: BLE001 — ohne Backup-Integration gibt es nichts aufzuräumen
            manager, local_id = None, None

        if job is None or self._is_running(str(job.get("request_id"))):
            if manager is not None and local_id is not None and job is None:
                await self._prune_local(manager, local_id, keep_backup_id=None)
            return

        request_id = str(job["request_id"])
        if job.get("phase") != PHASE_UPLOADING or not job.get("backup_id"):
            # Mitten im Erzeugen neu gestartet: Ob HA fertig wurde, ist offen — neu anfangen
            # muss der Integrator. Ein fertiges Backup bleibt und zählt zum Bestand.
            _LOGGER.info("Backup-Auftrag %s war beim Neustart im Erzeugen — beendet", request_id)
            await self._report_failed(request_id, BackupJobFailed(ERROR_HA_RESTARTED))
            await self._finish(request_id, DONE_FAILED, error=ERROR_HA_RESTARTED)
            if manager is not None and local_id is not None:
                await self._prune_local(manager, local_id, keep_backup_id=None)
            return

        if manager is None or local_id is None:
            await self._report_failed(request_id, BackupJobFailed(ERROR_LOCAL_AGENT_MISSING))
            await self._finish(request_id, DONE_FAILED, error=ERROR_LOCAL_AGENT_MISSING)
            return
        await self._prune_local(manager, local_id, keep_backup_id=str(job["backup_id"]))
        self._start(self._resume_upload(manager, local_id, job), request_id=request_id)

    async def _resume_upload(self, manager: Any, local_id: str, job: dict[str, Any]) -> None:
        """Setzt einen Upload nach dem Neustart fort. Das Backup bleibt in jedem Ausgang in
        HA — als verschlüsselt geprüft war es schon vor dem Neustart."""
        request_id = str(job["request_id"])
        backup_id = str(job["backup_id"])
        size = int(job.get("size") or 0)
        chunk_bytes = _positive_int(job.get("chunk_bytes"), BACKUP_CHUNK_BYTES)
        try:
            state = None
            for attempt in range(BACKUP_RESUME_ATTEMPTS):
                state = await self._server_state(request_id)
                if state is not None:
                    break
                if attempt + 1 < BACKUP_RESUME_ATTEMPTS:
                    await asyncio.sleep(BACKUP_RESUME_RETRY_SECONDS)
            if state is None:
                raise BackupJobAborted("Backend nach dem Neustart nicht erreichbar")
            status = str(state.get("status") or "")
            if status == "closed":
                raise BackupJobAborted("request_closed")
            if status in ("ready", "downloaded"):
                _LOGGER.info("Backup-Auftrag %s war schon abgeschlossen", request_id)
                await self._finish(request_id, DONE_READY, size=size)
                return
            if status not in ("uploading", "creating"):
                raise BackupJobAborted(f"unerwarteter Zustand {status}")
            if await self._local_backup(manager, local_id, backup_id) is None:
                raise BackupJobFailed(ERROR_BACKUP_NOT_FOUND)
            offset = _positive_int(state.get("receivedBytes"), 0)
            if status == "creating":
                # Die Meldung „uploading“ kam vor dem Neustart nicht mehr an — nachholen,
                # bei Notfallkit mit dem neu aus HA gelesenen Schlüssel (#203).
                await self._report_uploading(manager, request_id, job)
                offset = 0
            _LOGGER.info("Backup-Auftrag %s: Upload nach Neustart ab %d Byte fortgesetzt", request_id, offset)
            sha256 = await self._upload(manager, local_id, request_id, backup_id, size, chunk_bytes, offset)
            await self._complete(request_id, sha256, size)
            await self._finish(request_id, DONE_READY, sha256=sha256, size=size)
        except BackupJobFailed as err:
            _LOGGER.warning("Backup-Auftrag %s fehlgeschlagen: %s", request_id, err)
            await self._report_failed(request_id, err)
            await self._finish(request_id, DONE_FAILED, error=err.code)
        except BackupJobAborted as err:
            _LOGGER.info("Backup-Auftrag %s beendet: %s", request_id, err)
            await self._finish(request_id, DONE_CLOSED)
        except asyncio.CancelledError:
            if request_id in self._superseded:
                self._superseded.discard(request_id)
                await self._finish(request_id, DONE_CLOSED)
            raise

    async def _local_backup(self, manager: Any, local_id: str, backup_id: str) -> Any | None:
        for backup in await manager.backup_agents[local_id].async_list_backups():
            if getattr(backup, "backup_id", None) == backup_id:
                return backup
        return None

    # ------------------------------------------------------------------ Aufräumen

    async def _prune_local(self, manager: Any, local_id: str, *, keep_backup_id: str | None) -> None:
        """Behält von den lokalen Backups mit unserer Markierung nur die neuesten
        ``BACKUP_KEEP_LOCAL`` — das des laufenden Auftrags immer und zuerst. Markierte
        Backups ohne Verschlüsselung gehen immer. Backups ohne Markierung gehören dem Kunden
        und bleiben unberührt; erkannt wird nur an der Markierung, nie am Namen."""
        try:
            backups = await manager.backup_agents[local_id].async_list_backups()
        except Exception:  # noqa: BLE001 — Aufräumen darf nie einen Auftrag verhindern
            _LOGGER.debug("Lokale Backups nicht lesbar — kein Aufräumen", exc_info=True)
            return
        ours = [
            b for b in backups
            if BACKUP_METADATA_KEY in (getattr(b, "extra_metadata", None) or {})
            and getattr(b, "backup_id", None)
        ]
        # Neueste zuerst; das laufende steht davor, egal welches Datum es trägt.
        ours.sort(key=_backup_timestamp, reverse=True)
        ours.sort(key=lambda b: b.backup_id != keep_backup_id)
        kept = 0
        for backup in ours:
            backup_id = backup.backup_id
            if backup_id == keep_backup_id:
                kept += 1
                continue
            if not getattr(backup, "protected", True):
                _LOGGER.info("Unverschlüsseltes Fleet-Manager-Backup %s wird gelöscht", backup_id)
            elif kept < BACKUP_KEEP_LOCAL:
                kept += 1
                continue
            else:
                _LOGGER.info("Älteres Fleet-Manager-Backup %s wird gelöscht — es bleiben die neuesten %d",
                             backup_id, BACKUP_KEEP_LOCAL)
            await self._delete_local(manager, local_id, backup_id)

    async def _delete_local(self, manager: Any, local_id: str | None, backup_id: str | None) -> None:
        if manager is None or local_id is None or not backup_id:
            return
        try:
            errors = await manager.async_delete_backup(backup_id, agent_ids=[local_id])
            if errors:
                _LOGGER.warning("Lokales Backup %s nicht vollständig gelöscht: %s", backup_id, errors)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Lokales Backup %s konnte nicht gelöscht werden", backup_id, exc_info=True)

    # ------------------------------------------------------------------ Zustand

    async def _save_job(self, job: dict[str, Any] | None) -> None:
        self._job = job
        await self._store.async_save({"job": job, "done": self._done})

    async def _finish(self, request_id: str, status: str, *, error: str | None = None,
                      sha256: str | None = None, size: int | None = None) -> None:
        entry: dict[str, Any] = {"request_id": request_id, "status": status}
        if error:
            entry["error"] = error
        if sha256:
            entry["sha256"] = sha256
        if size is not None:
            entry["size"] = size
        self._done = [d for d in self._done if d.get("request_id") != request_id]
        self._done.append(entry)
        self._done = self._done[-BACKUP_DONE_HISTORY:]
        job = self._job if self._job and self._job.get("request_id") != request_id else None
        await self._save_job(job)


# ---------------------------------------------------------------------- Helfer


def _parse_settings(raw: Any) -> dict[str, Any]:
    """``settings`` aus dem Poll (camelCase) in die interne Form. Fehlt es — älteres
    Backend —, gilt die Vorgabe: wie die automatischen Backups, ohne Notfallkit."""
    if not isinstance(raw, dict):
        raw = {}

    def _strings(value: Any) -> list[str]:
        return [str(v) for v in value] if isinstance(value, list) else []

    return {
        "mode": str(raw.get("mode") or MODE_AUTOMATIC).strip().lower(),
        "include_database": bool(raw.get("includeDatabase", True)),
        "include_folders": _strings(raw.get("includeFolders")),
        "include_all_addons": bool(raw.get("includeAllAddons", False)),
        "include_addons": _strings(raw.get("includeAddons")),
        "include_emergency_kit": bool(raw.get("includeEmergencyKit", False)),
    }


def _key_hash(key: str | None) -> str:
    """SHA-256 des Schlüssels — nur dieser steht im Store, nie der Schlüssel selbst."""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _backup_timestamp(backup: Any) -> float:
    """Zeitpunkt aus ``AgentBackup.date`` (ISO-String) zum Sortieren; unlesbar zählt als ältestes."""
    try:
        return datetime.fromisoformat(str(getattr(backup, "date", ""))).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _retry_after(resp: Any) -> float:
    headers = getattr(resp, "headers", None) or {}
    try:
        return max(1.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return float(BACKUP_DEFAULT_RETRY_AFTER_SECONDS)


async def _json_body(resp: Any) -> dict[str, Any]:
    try:
        payload = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — Fehlerseiten ohne JSON
        return {}
    return payload if isinstance(payload, dict) else {}


def _short(err: Any) -> str:
    return _one_line(str(err))[:MAX_DETAIL_LEN]


def _one_line(text: str) -> str:
    return " ".join(str(text).split())
