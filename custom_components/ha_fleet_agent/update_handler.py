"""UpdateCommandHandler — fuehrt vom Backend angestossene Update-Befehle aus (#103).

Der RequestPoller liefert in einem ``update_batch``-Tick eine Liste offener
Update-Commands (Plan §4.2). Dieser Handler arbeitet sie **sequenziell** ab: pro
Command ein nicht-blockierender ``update.install``-Service-Call, danach sofort ein
Report ans Backend (``started`` | ``failed``). Ein Fehler pro Command bricht die
Kette **nicht** ab — die restlichen Commands laufen weiter.

Fortschritt/Ergebnis wird hier NICHT abgewartet (``update.install`` ist
nicht-blockierend, Research §6): Den Abschluss erkennt das Backend am naechsten
60-s-State-Push, sobald die Ziel-``update``-Entity nicht mehr ``update_available``
ist. Der serverseitige Watchdog re-queued Commands, die nie quittiert werden.
"""

from __future__ import annotations

import logging
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

    async def handle(self, data: dict[str, Any]) -> None:
        """Poll-Handler fuer ``action == "update_batch"``.

        Erwartet ``data["commands"]`` = Liste von
        ``{commandId, entity_id, version?, backup?}``. Arbeitet sie sequenziell ab;
        jeder Command wird einzeln ausgefuehrt und quittiert.
        """
        commands = data.get("commands")
        if not isinstance(commands, list) or not commands:
            _LOGGER.debug("update_batch ohne Commands — nichts zu tun")
            return

        _LOGGER.info("update_batch: %d Command(s) werden abgearbeitet", len(commands))
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

        try:
            # blocking=False: update.install ist langlaufend (Add-on-Pull, Core-
            # Reboot). Wir warten NICHT — sonst blockiert ein Command den ganzen
            # Batch und den naechsten Poll-Tick. Abschluss kommt via State-Push.
            await self._hass.services.async_call(
                "update", "install", service_data, blocking=False
            )
        except Exception as err:  # noqa: BLE001 — ein Command-Fehler darf die Kette nicht stoppen
            _LOGGER.warning(
                "update.install fuer %s fehlgeschlagen: %s", entity_id, err
            )
            await self._report(command_id, REPORT_FAILED, str(err))
            return

        _LOGGER.info(
            "update.install ausgeloest fuer %s (command=%s)", entity_id, command_id
        )
        await self._report(command_id, REPORT_STARTED)

    async def _report(
        self, command_id: str, status: str, error: str | None = None
    ) -> None:
        """Quittiert einen Command ans Backend (``POST .../report``).

        Serverseitig idempotent. Ein fehlgeschlagener Report wird nur geloggt —
        notfalls schliesst der naechste State-Push den Command ueber den
        Versionsstand bzw. der Watchdog re-queued ihn.
        """
        url = f"{self._backend_url}/api/agent/update-commands/{command_id}/report"
        body: dict[str, Any] = {"status": status}
        if error:
            body["error"] = error[:MAX_ERROR_LEN]
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=10)
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
                else:
                    _LOGGER.warning(
                        "Command-Report %s fehlgeschlagen (HTTP %d)",
                        command_id,
                        resp.status,
                    )
        except Exception as err:  # noqa: BLE001 — Report-Fehler dürfen nichts crashen
            _LOGGER.warning("Command-Report %s Netzwerkfehler: %s", command_id, err)
