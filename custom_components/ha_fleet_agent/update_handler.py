"""UpdateCommandHandler — fuehrt vom Backend angestossene Update-Befehle aus (#103).

Der RequestPoller liefert in einem ``update_batch``-Tick eine Liste offener
Update-Commands (Plan §4.2). Dieser Handler arbeitet sie **sequenziell** ab: pro
Command ein nicht-blockierender ``update.install``-Service-Call, danach sofort ein
Report ans Backend (``started`` | ``failed``). Ein Fehler pro Command bricht die
Kette **nicht** ab — die restlichen Commands laufen weiter.

Die Auslieferung ist **at-least-once**: geht die Quittierung verloren, stellt der
serverseitige Watchdog den Command nach 5 Minuten zurueck auf ``PENDING`` und der
naechste Poll liefert ihn erneut. Die Gegenseite dazu ist der Idempotenz-Riegel in
diesem Handler (:attr:`UpdateCommandHandler._executed`): ein bereits ausgefuehrter
``commandId`` wird nur noch quittiert, nicht ein zweites Mal installiert.

Den **Abschluss** erkennt das Backend am naechsten 60-s-State-Push, sobald die
Ziel-``update``-Entity ihr Ziel meldet. Ein **Fehlschlag** dagegen wird hier gemeldet:
Der Batch laeuft in einem eigenen Hintergrund-Task, darin jeder ``update.install`` mit
``blocking=True``. Frueher lief der Call mit ``blocking=False`` — dann kehrte er sofort
zurueck, und eine Exception aus HACS/Supervisor verschwand spurlos in Home Assistant:
Die App zeigte weiter „laeuft"/„Neustart erforderlich", obwohl nie etwas installiert
wurde. Der Hintergrund-Task haelt zugleich den Poll-Tick frei (der Poller ist
reentrancy-geschuetzt, ein Add-on-Pull wuerde ihn sonst minutenlang blockieren).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Report-Status, die das Plugin synchron meldet. Den Abschluss (COMPLETED) leitet
# das Backend aus dem State-Push ab — nicht von hier.
REPORT_STARTED = "started"
REPORT_FAILED = "failed"

# Fehlertext im Report defensiv kuerzen (Backend-Spalte + Log nicht aufblaehen).
MAX_ERROR_LEN = 500

# Quittierung wiederholen (#142, B7): ein einzelner verlorener POST liess den Command
# im Backend auf DISPATCHED stehen — der Watchdog lieferte ihn danach erneut aus. Drei
# Versuche mit kurzem Backoff, Gesamtbudget rund 30 s (3 x 8 s Timeout + 2 s + 4 s Pause).
REPORT_TIMEOUT_SECONDS = 8
REPORT_BACKOFF_SECONDS = (2, 4)

# Lebensdauer des Idempotenz-Riegels (#142, B3). Muss ueber der Watchdog-Schwelle des
# Backends (5 min) liegen, damit ein re-dispatchter Command sicher noch als „schon
# ausgefuehrt" erkannt wird. Der Riegel lebt nur im Speicher: ein HA-Neustart leert ihn —
# das ist hingenommen, denn nach einem Neustart ist eine erneute Installation kein
# ueberlappender Doppelvorgang mehr.
EXECUTED_TTL_SECONDS = 15 * 60


class UpdateCommandHandler:
    """Verarbeitet die ``update_batch``-Poll-Aktion (#103)."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        backend_url: str,
        api_key: str,
    ) -> None:
        self._hass = hass
        self._session = session
        self._backend_url = backend_url.rstrip("/")
        self._api_key = api_key
        # Idempotenz-Riegel (#142, B3): commandId -> monotone Zeit der Ausfuehrung.
        self._executed: dict[str, float] = {}

    async def handle(self, data: dict[str, Any]) -> None:
        """Poll-Handler fuer ``action == "update_batch"``.

        Erwartet ``data["commands"]`` = Liste von
        ``{commandId, entity_id, version?, backup?}``. Startet die Abarbeitung als
        Hintergrund-Task und kehrt sofort zurueck — die Commands laufen darin
        sequenziell, jeder wird einzeln ausgefuehrt und quittiert.
        """
        commands = data.get("commands")
        if not isinstance(commands, list) or not commands:
            _LOGGER.debug("update_batch ohne Commands — nichts zu tun")
            return

        _LOGGER.info("update_batch: %d Command(s) werden abgearbeitet", len(commands))
        # Eigener Task: ``update.install`` laeuft blockierend (nur so bekommen wir einen
        # Fehlschlag mit), darf aber den Poll-Tick nicht aufhalten.
        self._hass.async_create_task(
            self._run_batch(commands), name="hafm_update_batch"
        )

    async def _run_batch(self, commands: list[Any]) -> None:
        """Arbeitet die Commands des Batches sequenziell ab."""
        for cmd in commands:
            if isinstance(cmd, dict):
                await self._run_one(cmd)

    async def _run_one(self, cmd: dict[str, Any]) -> None:
        """Fuehrt genau einen Update-Command aus und quittiert ihn.

        Liest die Felder tolerant in camelCase **und** snake_case, damit das
        Plugin gegen beide Backend-JSON-Konventionen immun bleibt. Ein
        ``update.install``-Fehler wird abgefangen, als ``failed`` gemeldet und
        stoppt die uebrigen Commands **nicht**.
        """
        command_id = cmd.get("commandId") or cmd.get("command_id")
        entity_id = cmd.get("entityId") or cmd.get("entity_id")
        if not command_id or not entity_id:
            _LOGGER.warning(
                "update_batch: Command ohne commandId/entity_id — uebersprungen: %s", cmd
            )
            return

        # Service-Daten: entity_id immer; version/backup NUR wenn gesetzt.
        # update.install lehnt nicht unterstuetzte Optionen sonst ab — Add-ons
        # koennen z.B. kein SPECIFIC_VERSION (Research §3).
        service_data: dict[str, Any] = {"entity_id": entity_id}
        version = cmd.get("version")
        if version:
            service_data["version"] = version
        if cmd.get("backup"):
            service_data["backup"] = True

        # Idempotenz-Riegel (#142, B3): Die Auslieferung ist at-least-once. Ging die
        # Quittierung verloren, stellt der Watchdog den Command nach 5 min auf PENDING
        # zurueck und der naechste Poll bringt ihn erneut — frueher lief update.install
        # dann ein zweites Mal. Systematisch traf das „Alle updaten": der Batch arbeitet
        # sequenziell mit blocking=True, und ein Add-on, das minutenlang ein Image zieht,
        # schiebt die Quittierung aller nachfolgenden Commands ueber die Schwelle.
        # Jetzt wird ein bekannter Command nur noch quittiert.
        self._forget_expired()
        if command_id in self._executed:
            _LOGGER.info(
                "Command %s wurde bereits ausgefuehrt — nur quittieren, kein zweites "
                "update.install fuer %s",
                command_id,
                entity_id,
            )
            await self._report(command_id, REPORT_STARTED)
            return
        # VOR dem Service-Call vermerken: ein waehrend der Installation erneut
        # ausgelieferter Command darf ebenso wenig ein zweites Mal starten.
        self._executed[command_id] = time.monotonic()

        # „started" sofort, bevor der Call laeuft: die App soll den Zustand direkt
        # zeigen, nicht erst nach Minuten. Ein spaeterer Fehlschlag korrigiert ihn.
        _LOGGER.info(
            "update.install ausgeloest fuer %s (command=%s)", entity_id, command_id
        )
        await self._report(command_id, REPORT_STARTED)

        try:
            # blocking=True: nur so wirft ein fehlgeschlagenes update.install auch bei
            # uns — mit blocking=False blieb der Fehlschlag in HA und der Command in der
            # App bis zum serverseitigen Timeout auf „laeuft". Den Poll-Tick haelt das
            # nicht auf, wir laufen bereits im Hintergrund-Task (s. handle()).
            await self._hass.services.async_call(
                "update", "install", service_data, blocking=True
            )
        except asyncio.CancelledError:
            # HA faehrt herunter — bei Core/OS-Updates der Normalfall, genau darauf
            # zielt das Update ja ab. Kein Fehlschlag: den Abschluss klaert der
            # State-Push nach dem Neustart.
            _LOGGER.debug(
                "update.install fuer %s abgebrochen (HA-Neustart?) — kein Fehlerreport",
                entity_id,
            )
            raise
        except Exception as err:  # noqa: BLE001 — ein Command-Fehler darf die Kette nicht stoppen
            _LOGGER.warning(
                "update.install fuer %s fehlgeschlagen: %s", entity_id, err
            )
            await self._report(command_id, REPORT_FAILED, str(err))
            return

        _LOGGER.debug(
            "update.install fuer %s zurueckgekehrt (command=%s)", entity_id, command_id
        )

    def _forget_expired(self) -> None:
        """Raeumt abgelaufene Eintraege des Idempotenz-Riegels.

        Ohne das Aufraeumen waere die Merkliste ein langsames Speicherleck — sie
        braucht nur so lange zu tragen, wie das Backend re-dispatchen kann.
        """
        cutoff = time.monotonic() - EXECUTED_TTL_SECONDS
        for command_id in [k for k, at in self._executed.items() if at < cutoff]:
            del self._executed[command_id]

    async def _report(
        self, command_id: str, status: str, error: str | None = None
    ) -> None:
        """Quittiert einen Command ans Backend (``POST .../report``).

        Serverseitig idempotent, darum ist ein Wiederholungsversuch gefahrlos — und
        noetig: frueher wurde ein verlorener POST nur geloggt, der Command blieb im
        Backend auf ``DISPATCHED`` und wurde nach der Watchdog-Schwelle erneut
        ausgeliefert. Drei Versuche mit kurzem Backoff (#142, B7).

        Wiederholt wird nur, was sich wiederholen laesst: Netzwerkfehler, Timeouts und
        5xx. Ein 4xx ist eine Aussage des Backends (z.B. Command unbekannt) — die
        aendert sich beim zweiten Anlauf nicht. Bleibt auch der letzte Versuch erfolglos,
        traegt weiterhin die Selbstheilung: der naechste State-Push schliesst den Command
        ueber den Versionsstand ab, und der Riegel oben verhindert die Doppelausfuehrung.
        """
        url = f"{self._backend_url}/api/agent/update-commands/{command_id}/report"
        body: dict[str, Any] = {"status": status}
        if error:
            body["error"] = error[:MAX_ERROR_LEN]
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=REPORT_TIMEOUT_SECONDS)
        attempts = len(REPORT_BACKOFF_SECONDS) + 1

        for attempt in range(attempts):
            try:
                async with self._session.post(
                    url, json=body, headers=headers, timeout=timeout
                ) as resp:
                    if 200 <= resp.status < 300:
                        _LOGGER.debug(
                            "Command %s quittiert (status=%s, HTTP %d)",
                            command_id,
                            status,
                            resp.status,
                        )
                        return
                    if resp.status < 500:
                        _LOGGER.warning(
                            "Command-Report %s abgelehnt (HTTP %d) — kein erneuter Versuch",
                            command_id,
                            resp.status,
                        )
                        return
                    _LOGGER.warning(
                        "Command-Report %s fehlgeschlagen (HTTP %d, Versuch %d/%d)",
                        command_id,
                        resp.status,
                        attempt + 1,
                        attempts,
                    )
            except asyncio.CancelledError:
                # HA faehrt herunter — nicht in eine Retry-Schleife zwingen.
                raise
            except Exception as err:  # noqa: BLE001 — Report-Fehler dürfen nichts crashen
                _LOGGER.warning(
                    "Command-Report %s Netzwerkfehler (Versuch %d/%d): %s",
                    command_id,
                    attempt + 1,
                    attempts,
                    err,
                )
            if attempt < len(REPORT_BACKOFF_SECONDS):
                await asyncio.sleep(REPORT_BACKOFF_SECONDS[attempt])

        _LOGGER.warning(
            "Command-Report %s nach %d Versuchen aufgegeben (status=%s)",
            command_id,
            attempts,
            status,
        )
