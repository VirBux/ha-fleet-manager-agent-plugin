"""RestartHandler — startet Home Assistant auf Backend-Befehl neu (#127, Sofort-Weg).

Der RequestPoller liefert in einem ``restart``-Tick die Bitte, das System neu zu
starten (schlankes Flag im Backend, kein Command-Lifecycle — beim Ausliefern
konsumiert). Dieser Handler ruft den offiziellen HA-Service
``homeassistant.restart`` auf.

Zwei Besonderheiten gegenueber dem ClearLogsHandler:

* ``blocking=False`` — der Service beendet den laufenden Prozess; mit ``blocking=True``
  wuerde der Aufruf mitten im Neustart haengen bzw. mit einem Abbruch-Fehler enden,
  der faelschlich als Fehlschlag im Log landet.
* **Kein State-Push danach.** Das System geht offline; der Backend-Snapshot bleibt
  auf dem letzten Stand stehen, bis sich der Agent nach dem Neustart selbst wieder
  meldet (das ist die gewuenschte Anzeige „gerade offline").

Alle Fehler werden nur geloggt, nie geworfen — der Poll-Dispatch darf nicht crashen.
Faellt der Aufruf aus, passiert nichts; der Integrator loest bei Bedarf erneut aus.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class RestartHandler:
    """Verarbeitet die ``restart``-Poll-Aktion (#127)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def handle(self, data: dict[str, Any]) -> None:
        """Poll-Handler fuer ``action == "restart"``.

        Die Aktion traegt keine Nutzdaten — ``data`` wird nur fuer die einheitliche
        Handler-Signatur entgegengenommen.
        """
        _LOGGER.warning(
            "System-Neustart auf Backend-Befehl: rufe homeassistant.restart auf"
        )
        try:
            await self._hass.services.async_call(
                "homeassistant", "restart", {}, blocking=False
            )
        except Exception as err:  # noqa: BLE001 — ein Service-Fehler darf den Poll nicht crashen
            _LOGGER.warning("homeassistant.restart fehlgeschlagen: %s", err)
