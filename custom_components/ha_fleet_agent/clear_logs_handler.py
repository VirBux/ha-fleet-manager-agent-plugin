"""ClearLogsHandler — leert auf Backend-Befehl den HA-system_log-Puffer (#109).

Der RequestPoller liefert in einem ``clear_logs``-Tick die Bitte, die HA-Logs zu
leeren (schlankes Flag im Backend, kein Command-Lifecycle). Dieser Handler ruft den
offiziellen HA-Service ``system_log.clear`` auf — der leert den GESAMTEN Ringpuffer
(ERROR/CRITICAL **und** WARNING gemeinsam; HA bietet kein selektives Loeschen
einzelner Eintraege). Danach stoesst er einen sofortigen State-Push an, damit der
Backend-Snapshot und die Dashboard-Cards ohne "Wiederaufblitzen" leer werden (sonst
zoege erst der naechste 60-s-Push nach).

Alle Fehler werden nur geloggt, nie geworfen — der Poll-Dispatch (mit eigenem
Reentrancy-Guard) darf nicht crashen. Faellt der clear aus, bleibt der Puffer
gefuellt; der Integrator loest bei Bedarf erneut aus (Backend-Flag, idempotent).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .state_reporter import StateReporter

_LOGGER = logging.getLogger(__name__)


class ClearLogsHandler:
    """Verarbeitet die ``clear_logs``-Poll-Aktion (#109)."""

    def __init__(self, hass: HomeAssistant, state_reporter: StateReporter) -> None:
        self._hass = hass
        self._state_reporter = state_reporter

    async def handle(self, data: dict[str, Any]) -> None:
        """Poll-Handler fuer ``action == "clear_logs"``.

        Die Aktion traegt keine Nutzdaten — ``data`` wird nur fuer die einheitliche
        Handler-Signatur entgegengenommen.
        """
        try:
            # blocking=True: system_log.clear leert nur den In-Memory-Ringpuffer
            # (schnell) — so bekommen wir ein evtl. Fehlerergebnis direkt zurueck.
            await self._hass.services.async_call(
                "system_log", "clear", {}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 — ein Service-Fehler darf den Poll nicht crashen
            _LOGGER.warning("system_log.clear fehlgeschlagen: %s", err)
            return

        _LOGGER.info("HA-Logs geleert (system_log.clear) auf Backend-Befehl")

        # Sofortigen State-Push anstossen, damit der Backend-Snapshot (und die
        # Dashboard-Cards) nicht bis zum naechsten 60-s-Tick gefuellt bleiben.
        try:
            await self._state_reporter.push_now()
        except Exception as err:  # noqa: BLE001 — best effort, naechster Tick zieht ohnehin nach
            _LOGGER.debug("Sofort-Push nach clear_logs fehlgeschlagen: %s", err)
