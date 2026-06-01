"""StateReporter — sendet den HA-State-Payload per REST ans Backend (TODO #50.19).

Sendet alle 60 s via POST /api/agent/state mit X-API-Key-Header.
Der Payload-Aufbau ist identisch mit der früheren WebSocket-Variante — nur der
Transport ändert sich: kein WebSocket mehr, sondern aiohttp.ClientSession.

Fehlerverhalten:
- 5xx / Timeout: warn-loggen, kein Crash, nächster Tick versucht es erneut.
- 4xx (401/403): warn-loggen, Ticker läuft weiter (User korrigiert Key im Config-Flow).
- Netzwerkfehler: wie 5xx behandeln.

Das SIGNAL_CONNECTION_STATE-Signal wird nach jedem Versuch gesendet:
True bei HTTP 2xx, False bei Fehler — damit der Sensor in der UI den
"letzten bekannten Status" zeigt.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from typing import Any

import aiohttp
from homeassistant.const import __version__ as HA_CORE_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    ERROR_LOG_LEVELS,
    MAX_ERROR_LOG_MESSAGE_LEN,
    MAX_ERROR_LOGS,
    MAX_WARNING_LOGS,
    SIGNAL_CONNECTION_STATE,
    STATE_UPDATE_INTERVAL_SECONDS,
    WARNING_LOG_LEVELS,
)

_LOGGER = logging.getLogger(__name__)


class StateReporter:
    """Erhebt periodisch den HA-Systemzustand und sendet ihn per REST ans Backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        session: aiohttp.ClientSession,
        backend_url: str,
        api_key: str,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._session = session
        # Endpoint: POST {backend_url}/api/agent/state
        self._endpoint = backend_url.rstrip("/") + "/api/agent/state"
        self._api_key = api_key
        self._started_at = time.monotonic()
        self._unsub_interval = None

    def start(self) -> None:
        """Sofort einen Payload senden und das Intervall registrieren."""
        self._unsub_interval = async_track_time_interval(
            self._hass,
            self._tick,
            datetime.timedelta(seconds=STATE_UPDATE_INTERVAL_SECONDS),
        )
        # Erstes Payload sofort — nicht erst nach 60 s warten
        self._hass.async_create_task(self._push_once())

    def stop(self) -> None:
        """Stoppt den periodischen Timer."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    # --------------------------------------------------------- Intervall

    async def _tick(self, _now: Any) -> None:
        await self._push_once()

    async def _push_once(self) -> None:
        """Baut den Payload und sendet ihn per HTTP POST."""
        try:
            payload = await self._build_payload()
        except Exception:  # noqa: BLE001 — Sammeln darf nie hart fehlschlagen
            _LOGGER.exception("Aufbau des State-Payloads ist fehlgeschlagen")
            return

        success = await self._post(payload)
        # Sensor-Update: True = letzter Post erfolgreich
        async_dispatcher_send(
            self._hass, SIGNAL_CONNECTION_STATE, self._entry_id, success
        )

    async def _post(self, payload: dict[str, Any]) -> bool:
        """Sendet den Payload ans Backend. Gibt True bei HTTP 2xx zurück."""
        headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._session.post(
                self._endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if 200 <= resp.status < 300:
                    _LOGGER.debug("State-Payload gesendet (HTTP %d)", resp.status)
                    return True
                if resp.status in (401, 403):
                    _LOGGER.warning(
                        "Backend lehnte State-Push ab (HTTP %d) — API-Key prüfen",
                        resp.status,
                    )
                else:
                    _LOGGER.warning(
                        "State-Push fehlgeschlagen (HTTP %d) — nächster Tick wiederholt",
                        resp.status,
                    )
                return False
        except asyncio.TimeoutError:
            _LOGGER.warning("State-Push Timeout (>10 s) — nächster Tick wiederholt")
            return False
        except aiohttp.ClientError as err:
            _LOGGER.warning("State-Push Netzwerkfehler: %s", err)
            return False

    # --------------------------------------------------------- Payload-Aufbau
    # (Transport-unabhängig — Bugfix nach Bug-Report 2026-05-24)

    async def _build_payload(self) -> dict[str, Any]:
        """Erstellt den State-Payload gemäß REQUIREMENTS §5.2.

        Felder `type` und `api_key` entfallen (REST-Transport trägt sie im Header).

        Quellen-Wahl für CPU/RAM (Bugfix 2026-05-24, Stand 0.2.3):
        Primär `psutil.cpu_percent()` + `psutil.virtual_memory().percent` —
        liefert **Host-Werte** (wie HA-UI sie unter Settings → System anzeigt).
        Im HA-Core-Container ist /proc/meminfo + /proc/stat host-weit sichtbar,
        weil keine Cgroup-Limits gesetzt sind (memory.max = "max").
        Fallback: Supervisor `/core/stats` (Container-Werte) — nur wenn psutil
        nicht verfügbar.

        Disk-Quelle: Supervisor `/host/info` (disk_used / disk_total in MB) —
        misst die HAOS-Daten-Partition. Genauer als `df` im Container.
        """
        payload: dict[str, Any] = {
            "timestamp": dt_util.utcnow().isoformat().replace("+00:00", "Z"),
        }

        # ----- HA-eigene Daten (jedes Feld unabhängig in try/except) -----
        # ha_version: aus dem Modul-Konstanten `homeassistant.const.__version__` lesen.
        # `hass.config.version` ist je nach HA-Version nicht zuverlässig befüllt
        # (z. B. 2026.5.x liefert None vor vollständiger Config-Initialisierung).
        # Die Konstante ist dagegen ab Import sofort gesetzt — gleiche Quelle, die
        # `/api/config` an externe Clients zurückgibt. (#68)
        payload["ha_version"] = self._safe(
            lambda: self._stringify_version(HA_CORE_VERSION)
            or self._stringify_version(getattr(self._hass.config, "version", None))
        )
        payload["uptime_seconds"] = self._safe(
            lambda: int(time.monotonic() - self._started_at)
        )
        payload["entities_count"] = self._safe(
            lambda: len(self._hass.states.async_all())
        )
        payload["devices_count"] = self._safe(
            lambda: len(dr.async_get(self._hass).devices)
        )
        payload["automations_count"] = self._safe(
            lambda: len(self._hass.states.async_all("automation"))
        )
        payload["dashboards_count"] = self._safe(self._count_dashboards)
        payload["integrations"] = self._safe(self._list_integrations) or []
        payload["warnings"] = self._safe(self._count_persistent_notifications) or 0
        # Kritische Logs (ERROR/CRITICAL) aus HAs system_log als Snapshot (#65).
        # Loest den frueheren Post-MVP-Platzhalter (errors = 0) ab: errors ist jetzt
        # die Anzahl distinkter Fehler-Eintraege, error_logs traegt die Details.
        error_logs = self._safe(self._collect_error_logs) or []
        payload["error_logs"] = error_logs
        payload["errors"] = len(error_logs)
        # Warnungen (WARNING) als eigene Liste — getrennt von error_logs, damit
        # haeufige Warnungen die Fehler nicht aus dem Limit verdraengen. Eigene
        # warning_logs-Spalte im Backend.
        payload["warning_logs"] = self._safe(self._collect_warning_logs) or []

        # ----- Supervisor-API (nur HAOS) -----
        host_info, supervisor_info, core_stats = await self._fetch_supervisor_info()

        # CPU/RAM: psutil (Host-Werte) hat Vorrang, /core/stats nur als Fallback.
        cpu_value, ram_value, cpu_source, ram_source = await self._resolve_cpu_ram(core_stats)
        payload["cpu_percent"] = cpu_value
        payload["ram_percent"] = ram_value

        # Disk-Auslastung aus /host/info (disk_used/disk_total in MB).
        payload["disk_percent"] = self._safe(
            lambda: self._percent(host_info, "disk_used", "disk_total")
        )
        payload["ip"] = self._safe(lambda: self._resolve_ip(host_info))
        payload["addons"] = self._safe(
            lambda: self._list_started_addons(supervisor_info)
        ) or []

        # Diagnose-Log: ohne PII, Quelle pro Feld — hilft beim Bug-Triaging.
        _LOGGER.debug(
            "State-Payload aufgebaut — ha_version=%s cpu=%s(%s) ram=%s(%s) disk=%s ip=%s addons=%d",
            payload.get("ha_version"),
            payload.get("cpu_percent"),
            cpu_source,
            payload.get("ram_percent"),
            ram_source,
            payload.get("disk_percent"),
            payload.get("ip"),
            len(payload.get("addons", [])),
        )

        return payload

    async def _resolve_cpu_ram(
        self, core_stats: dict | None
    ) -> tuple[float | None, float | None, str, str]:
        """Wählt die Quelle für cpu_percent und ram_percent.

        Reihenfolge:
        1. psutil (Host-Werte) — primär, da HA-UI dieselbe Quelle nutzt.
        2. core_stats aus get_core_stats(hass) — Container-Fallback.
        3. Direkter Supervisor-API-Call /core/stats — letzter Fallback.

        Rückgabe: (cpu, ram, cpu_source, ram_source). Source-Strings nur fürs
        Debug-Log: "psutil" | "core_stats" | "supervisor_api" | "none".
        """
        host_stats = await self._fetch_host_stats_via_psutil()

        cpu_value: float | None = None
        ram_value: float | None = None
        cpu_source = "none"
        ram_source = "none"

        if host_stats:
            if host_stats.get("cpu_percent") is not None:
                cpu_value = float(host_stats["cpu_percent"])
                cpu_source = "psutil"
            if host_stats.get("memory_percent") is not None:
                ram_value = float(host_stats["memory_percent"])
                ram_source = "psutil"

        if cpu_value is not None and ram_value is not None:
            return cpu_value, ram_value, cpu_source, ram_source

        # psutil hat (mind. ein) Feld nicht geliefert — Container-Stats prüfen.
        if not (core_stats and core_stats.get("cpu_percent") is not None):
            api_stats = await self._fetch_core_stats_via_supervisor_api()
            fallback_source = "supervisor_api"
            stats = api_stats
        else:
            fallback_source = "core_stats"
            stats = core_stats

        if stats:
            if cpu_value is None and stats.get("cpu_percent") is not None:
                cpu_value = float(stats["cpu_percent"])
                cpu_source = fallback_source
            if ram_value is None and stats.get("memory_percent") is not None:
                ram_value = float(stats["memory_percent"])
                ram_source = fallback_source

        return cpu_value, ram_value, cpu_source, ram_source

    async def _fetch_host_stats_via_psutil(self) -> dict | None:
        """Liest CPU- und RAM-Auslastung des **Hosts** via psutil.

        Hintergrund: Im HA-Core-Container sind /proc/meminfo und /proc/stat
        host-weit sichtbar — der Container hat kein Cgroup-Memory-Limit
        (memory.max = "max"). Das ergibt z.B. für RAM exakt den Wert, den
        die HA-UI unter Settings → System anzeigt.

        psutil ist eine Pflicht-Dependency von Home Assistant Core (siehe
        homeassistant/package_constraints.txt), also ohne extra `requirements`
        im Manifest verfügbar. Auf Container-Setups ohne /proc-Zugriff fällt
        die Funktion still auf None zurück.

        Rückgabe: dict mit `cpu_percent` und `memory_percent` (jeweils float)
        oder None bei Fehler.
        """
        try:
            import psutil  # noqa: PLC0415 — Lazy-Import: nur wenn HAOS-Daten gebraucht werden
        except ImportError:
            _LOGGER.debug("psutil nicht importierbar — Fallback auf Supervisor-Stats")
            return None

        def _read() -> dict:
            # interval=None: nicht-blockierend, Mittelwert seit letztem Aufruf.
            # Erster Aufruf liefert daher 0.0 — beim 60s-Tick akzeptabel.
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            return {
                "cpu_percent": round(float(cpu), 1),
                "memory_percent": round(float(vm.percent), 1),
            }

        loop = asyncio.get_running_loop()
        try:
            stats = await loop.run_in_executor(None, _read)
            _LOGGER.debug(
                "psutil host stats: cpu=%s%% ram=%s%%",
                stats["cpu_percent"],
                stats["memory_percent"],
            )
            return stats
        except Exception:  # noqa: BLE001 — psutil-Fehler dürfen nichts crashen
            _LOGGER.debug("psutil Aufruf fehlgeschlagen", exc_info=True)
            return None

    # --------------------------------------------------------- Helper

    @staticmethod
    def _safe(fn: Any) -> Any:
        """Führt fn aus und gibt bei jedem Fehler None zurück."""
        try:
            return fn()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Sammlung einzelnes Feld fehlgeschlagen", exc_info=True)
            return None

    @staticmethod
    def _stringify_version(raw: Any) -> str | None:
        """Konvertiert HA-Version robust zu String — egal ob str oder AwesomeVersion."""
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    def _count_dashboards(self) -> int | None:
        """Lovelace-Dashboards aus `hass.data` zählen (interne Struktur)."""
        lovelace = self._hass.data.get("lovelace")
        if lovelace is None:
            return None
        dashboards = getattr(lovelace, "dashboards", None)
        if dashboards is None and isinstance(lovelace, dict):
            dashboards = lovelace.get("dashboards")
        if dashboards is None:
            return None
        return len(dashboards)

    def _list_integrations(self) -> list[dict[str, str]]:
        """Integrationen samt Status — pro Domain aggregiert, eigene Domain ausgeklammert.

        Frueher wurde nur der Domain-Name geladener Integrationen gemeldet; gestoppte
        oder fehlerhafte Integrationen fielen still unter den Tisch. Jetzt wird pro
        Integration ein {domain, status} gemeldet, normalisiert auf drei UI-Zustaende:

        - ``"active"``  — laeuft (``ConfigEntryState.LOADED``).
        - ``"error"``   — Laden fehlgeschlagen (``SETUP_ERROR`` / ``SETUP_RETRY`` /
          ``MIGRATION_ERROR`` / ``FAILED_UNLOAD``).
        - ``"stopped"`` — installiert, aber nicht aktiv (vom Nutzer deaktiviert
          via ``disabled_by``, ``NOT_LOADED``, ``SETUP_IN_PROGRESS``, ...).

        Mehrere Config-Entries derselben Domain (z.B. zwei Hue-Bridges) werden zu
        EINEM Eintrag zusammengefasst; der schlechteste Status gewinnt
        (``error`` > ``stopped`` > ``active``), damit eine ausgefallene Instanz im
        Monitoring nicht von einer laufenden ueberdeckt wird.
        """
        from homeassistant.config_entries import ConfigEntryState  # noqa: PLC0415

        error_states = {
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.MIGRATION_ERROR,
            ConfigEntryState.FAILED_UNLOAD,
        }
        # Rang fuer die Aggregation: hoeher = schlimmer und gewinnt pro Domain.
        rank = {"active": 0, "stopped": 1, "error": 2}

        worst: dict[str, str] = {}
        for entry in self._hass.config_entries.async_entries():
            if entry.domain == DOMAIN:
                continue
            # disabled_by gesetzt = bewusst deaktiviert (laedt gar nicht erst).
            if getattr(entry, "disabled_by", None) is not None:
                status = "stopped"
            elif entry.state == ConfigEntryState.LOADED:
                status = "active"
            elif entry.state in error_states:
                status = "error"
            else:
                status = "stopped"
            if entry.domain not in worst or rank[status] > rank[worst[entry.domain]]:
                worst[entry.domain] = status

        return [
            {"domain": domain, "status": status}
            for domain, status in sorted(worst.items())
        ]

    def _count_persistent_notifications(self) -> int:
        """MVP: Alle Persistent Notifications als Warnung zählen."""
        notifications = self._hass.data.get("persistent_notification")
        if notifications is None:
            return 0
        if isinstance(notifications, dict):
            return len(notifications)
        items = getattr(notifications, "_persistent_notifications", None)
        if items is not None:
            return len(items)
        return 0

    def _collect_error_logs(self) -> list[dict[str, Any]]:
        """Liest die juengsten ERROR/CRITICAL-Eintraege aus HAs system_log (#65).

        Spec #65: „Fehler, nicht Warnungen" — daher nur ERROR/CRITICAL. Siehe
        {@link _collect_logs} fuer Quelle und Format.
        """
        return self._collect_logs(ERROR_LOG_LEVELS, MAX_ERROR_LOGS)

    def _collect_warning_logs(self) -> list[dict[str, Any]]:
        """Liest die juengsten WARNING-Eintraege aus HAs system_log.

        Eigene Pipeline parallel zu {@link _collect_error_logs} mit eigenem
        Limit, damit haeufige Warnungen die selteneren Fehler nicht aus dem
        Snapshot verdraengen. Quelle und Format identisch.
        """
        return self._collect_logs(WARNING_LOG_LEVELS, MAX_WARNING_LOGS)

    def _collect_logs(
        self, levels: tuple[str, ...], max_count: int
    ) -> list[dict[str, Any]]:
        """Liest die juengsten Log-Eintraege der gegebenen ``levels`` aus HAs system_log.

        HAs ``system_log``-Komponente haelt die letzten Log-Eintraege (WARNING+)
        in einem Ringpuffer im RAM — dieselbe Quelle, die die WS-API
        ``system_log/list`` und das Log-Panel im HA-Frontend speisen. Wir filtern
        auf ``levels`` und liefern einen kompakten Snapshot, neueste zuerst
        (auf ``max_count`` gekappt, Messages auf MAX_ERROR_LOG_MESSAGE_LEN).

        Greift bewusst direkt auf die interne Struktur zu (analog zu
        ``persistent_notification``/``lovelace``). Liefert eine leere Liste, wenn
        die Komponente nicht geladen ist oder die Struktur unerwartet aussieht —
        nie ein harter Fehler (zusaetzlich von ``_safe`` umschlossen).
        """
        handler = self._hass.data.get("system_log")
        records = getattr(handler, "records", None)
        to_list = getattr(records, "to_list", None)
        if not callable(to_list):
            return []

        result: list[dict[str, Any]] = []
        for entry in to_list():  # to_list() liefert neueste zuerst
            if not isinstance(entry, dict):
                continue
            level = entry.get("level")
            if level not in levels:
                continue
            result.append(
                {
                    "level": level,
                    "source": self._shorten_logger(entry.get("name")),
                    "message": self._first_message(entry.get("message"))[
                        :MAX_ERROR_LOG_MESSAGE_LEN
                    ],
                    "at": self._epoch_to_iso(entry.get("timestamp")),
                }
            )
            if len(result) >= max_count:
                break
        return result

    @staticmethod
    def _first_message(message: Any) -> str:
        """system_log haelt die Message als Liste (dedupliziert, neueste zuletzt)."""
        if isinstance(message, (list, tuple)) and message:
            return str(message[-1])
        if message is None:
            return ""
        return str(message)

    @staticmethod
    def _shorten_logger(name: Any) -> str:
        """Kuerzt einen HA-Logger-Namen auf die Integration/Quelle.

        ``homeassistant.components.knx.climate`` -> ``knx``
        ``custom_components.frigate.api``        -> ``frigate``
        ``homeassistant.core``                   -> ``core``
        ``homeassistant.setup``                  -> ``setup``
        """
        if not name:
            return "?"
        text = str(name)
        for marker in ("custom_components.", "homeassistant.components."):
            if marker in text:
                return text.split(marker, 1)[1].split(".", 1)[0]
        if text == "homeassistant" or text.startswith("homeassistant.core"):
            return "core"
        if text.startswith("homeassistant."):
            return text.split(".")[-1]
        return text.split(".", 1)[0]

    @staticmethod
    def _epoch_to_iso(timestamp: Any) -> str | None:
        """Epoch-Sekunden (float) aus system_log -> ISO-8601-UTC (mit ``Z``)."""
        if timestamp is None:
            return None
        try:
            return (
                dt_util.utc_from_timestamp(float(timestamp))
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    async def _fetch_core_stats_via_supervisor_api(self) -> dict | None:
        """Holt die HA-Core-Container-Stats direkt vom Supervisor.

        Endpoint: GET http://supervisor/core/stats
        Auth: Bearer-Token aus der Env-Variable SUPERVISOR_TOKEN (vom HAOS-
        Container injiziert). Funktioniert ohne Stats-Subscription, also
        verlässlicher als `hassio.get_core_stats(hass)`.

        Rückgabe: dict mit Feldern aus dem Supervisor-Stats-Modell, u.a.
        `cpu_percent` (float) und `memory_percent` (float). None bei Fehler.
        """
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            _LOGGER.debug("Kein SUPERVISOR_TOKEN — vermutlich kein HAOS-Setup")
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with self._session.get(
                "http://supervisor/core/stats",
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Supervisor /core/stats HTTP %d — CPU/RAM-Stats nicht verfügbar",
                        resp.status,
                    )
                    return None
                body = await resp.json()
                # Supervisor wickelt Antworten oft in {"data": {...}, "result": "ok"}.
                # Beide Schemata akzeptieren — defensiv.
                if isinstance(body, dict) and "data" in body and isinstance(body["data"], dict):
                    stats = body["data"]
                else:
                    stats = body if isinstance(body, dict) else None
                _LOGGER.debug(
                    "Supervisor /core/stats OK: cpu=%s mem%%=%s",
                    stats.get("cpu_percent") if isinstance(stats, dict) else "?",
                    stats.get("memory_percent") if isinstance(stats, dict) else "?",
                )
                return stats if isinstance(stats, dict) else None
        except asyncio.TimeoutError:
            _LOGGER.debug("Supervisor /core/stats Timeout")
            return None
        except aiohttp.ClientError as err:
            _LOGGER.debug("Supervisor /core/stats Netzwerkfehler: %s", err)
            return None

    async def _fetch_supervisor_info(
        self,
    ) -> tuple[dict | None, dict | None, dict | None]:
        """Lädt Host-, Supervisor- und Core-Stats — nur wenn HAOS aktiv ist.

        Rückgabe (host_info, supervisor_info, core_stats):
        - host_info: aus `get_host_info(hass)` — enthält hostname, kernel,
          disk_total, disk_used (alles in MB). **Kein** cpu_percent.
        - supervisor_info: aus `get_supervisor_info(hass)` — enthält addons-Liste.
        - core_stats: aus `get_core_stats(hass)` — enthält cpu_percent +
          memory_percent für den HA-Core-Container. Quelle für CPU/RAM-Anzeige.

        Bei nicht-HAOS-Setups gibt es (None, None, None) zurück.
        """
        is_hassio_fn = self._resolve_hassio_fn("is_hassio")
        if is_hassio_fn is None:
            _LOGGER.debug("hassio-Helfer nicht importierbar — kein HAOS-Detect möglich")
            return None, None, None
        try:
            if not is_hassio_fn(self._hass):
                _LOGGER.debug("is_hassio()=False — kein HAOS-Setup")
                return None, None, None
        except Exception:  # noqa: BLE001
            _LOGGER.debug("is_hassio()-Aufruf fehlgeschlagen", exc_info=True)
            return None, None, None

        get_host_info = self._resolve_hassio_fn("get_host_info")
        get_supervisor_info = self._resolve_hassio_fn("get_supervisor_info")
        get_core_stats = self._resolve_hassio_fn("get_core_stats")

        if get_host_info is None and get_supervisor_info is None and get_core_stats is None:
            _LOGGER.debug("Keine Supervisor-Helper-Funktionen verfügbar")
            return None, None, None

        loop = asyncio.get_running_loop()

        def _maybe_call(fn: Any, name: str) -> dict | None:
            if fn is None:
                return None
            try:
                result = fn(self._hass)
                _LOGGER.debug("%s OK (%d keys)", name, len(result) if isinstance(result, dict) else -1)
                return result
            except Exception:  # noqa: BLE001
                _LOGGER.debug("%s fehlgeschlagen", name, exc_info=True)
                return None

        host_info = await loop.run_in_executor(None, _maybe_call, get_host_info, "get_host_info")
        supervisor_info = await loop.run_in_executor(
            None, _maybe_call, get_supervisor_info, "get_supervisor_info"
        )
        core_stats = await loop.run_in_executor(
            None, _maybe_call, get_core_stats, "get_core_stats"
        )
        return host_info, supervisor_info, core_stats

    @staticmethod
    def _resolve_hassio_fn(name: str) -> Any:
        """Sucht eine Hassio-Helfer-Funktion in beiden bekannten Pfaden."""
        for module_path in (
            "homeassistant.components.hassio",
            "homeassistant.helpers.hassio",
        ):
            try:
                module = __import__(module_path, fromlist=[name])
            except Exception:  # noqa: BLE001
                continue
            fn = getattr(module, name, None)
            if fn is not None:
                return fn
        return None

    @staticmethod
    def _percent(host_info: dict | None, used_key: str, total_key: str) -> float | None:
        if not host_info:
            return None
        used = host_info.get(used_key)
        total = host_info.get(total_key)
        if used is None or not total:
            return None
        return round(float(used) / float(total) * 100.0, 1)

    def _resolve_ip(self, host_info: dict | None) -> str | None:
        if host_info and host_info.get("ip_address"):
            return host_info["ip_address"]
        api = getattr(self._hass.config, "api", None)
        if api is not None and getattr(api, "local_ip", None):
            return api.local_ip
        return None

    @staticmethod
    def _list_started_addons(supervisor_info: dict | None) -> list[str]:
        if not supervisor_info:
            return []
        addons = supervisor_info.get("addons") or []
        return [
            a.get("name")
            for a in addons
            if isinstance(a, dict)
            and a.get("state") == "started"
            and a.get("name")
        ]
