"""RestartHandler — startet das System auf Backend-Befehl neu (#127, Sofort-Weg).

Der RequestPoller liefert in einem ``restart``-Tick die Bitte, das System neu zu
starten (schlankes Flag im Backend, kein Command-Lifecycle — beim Ausliefern
konsumiert). Der Tick trägt seit #144 den **Umfang**:

* ``scope: "core"`` (Standard, Verhalten wie bisher) → ``homeassistant.restart``
* ``scope: "supervisor"`` → ``POST http://supervisor/supervisor/restart``: nur die
  Verwaltungsschicht. Home Assistant selbst läuft durch.
* ``scope: "host"`` → ``hassio.host_reboot``: das ganze Gerät startet neu.

Die beiden letzten brauchen einen Supervisor (HA OS/Supervised); fehlt er, meldet der
Handler ``failed`` mit einer nennbaren Ursache, statt ins Leere zu laufen. Ein fehlender
oder unbekannter Umfang gilt als ``core`` — der harmloseste Eingriff.

**Warum der Supervisor per HTTP und nicht per Service?** Die ``hassio``-Integration bietet
Services für Add-ons, Backups und den Host — für den Supervisor selbst gibt es **keinen**:
``hassio.supervisor_restart`` existiert nicht, der Aufruf endete in „Action not found".
Der offizielle Weg ist die Supervisor-API — dieselbe Quelle, aus der der StateReporter schon
die Core-Stats zieht: Bearer-Token aus ``SUPERVISOR_TOKEN`` (vom Supervisor in den Container
injiziert), Endpoint ``/supervisor/restart``.

**Zwei Aufruf-Muster.** ``core`` und ``host`` beenden den eigenen Prozess: dort geht die
Quittung *vor* dem Aufruf raus und der Service läuft ``blocking=False``. Beim
Supervisor-Neustart überlebt der Agent — dort wird erst ausgeführt und danach quittiert,
sodass ein Fehler des Supervisors als echter Fehlschlag ankommt statt als stille
Nicht-Wirkung.

Zwei Besonderheiten gegenueber dem ClearLogsHandler:

* ``blocking=False`` — der Service beendet den laufenden Prozess; mit ``blocking=True``
  wuerde der Aufruf mitten im Neustart haengen bzw. mit einem Abbruch-Fehler enden,
  der faelschlich als Fehlschlag im Log landet.
* **Kein State-Push danach.** Das System geht offline; der Backend-Snapshot bleibt
  auf dem letzten Stand stehen, bis sich der Agent nach dem Neustart selbst wieder
  meldet (das ist die gewuenschte Anzeige „gerade offline").

**Quittung (#127, Rueckmeldung).** Vor dem Service-Aufruf meldet der Handler
``restarting`` ans Backend — danach ist der Prozess weg und koennte nichts mehr
senden. Scheitert der Aufruf, geht ``failed`` samt Fehlertext raus. Die Quittung ist
nur ein Zwischenstand: den Beweis, dass wirklich neu gestartet wurde, zieht das
Backend aus der Uptime des naechsten State-Pushes.

Alle Fehler werden nur geloggt, nie geworfen — der Poll-Dispatch darf nicht crashen.
Faellt der Aufruf aus, passiert nichts; der Integrator loest bei Bedarf erneut aus.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Quittungs-Status ans Backend (POST /api/agent/restart-ack).
ACK_RESTARTING = "restarting"
ACK_FAILED = "failed"

# Umfang des Neustarts (#144) und der jeweils zuständige HA-Service.
SCOPE_CORE = "core"
SCOPE_SUPERVISOR = "supervisor"
SCOPE_HOST = "host"
# HA-Services gibt es nur fuer core und host — den Supervisor startet die Supervisor-API neu.
SERVICE_BY_SCOPE = {
    SCOPE_CORE: ("homeassistant", "restart"),
    SCOPE_HOST: ("hassio", "host_reboot"),
}

# Alle Umfaenge, die dieser Handler kennt (Reihenfolge = steigende Eingriffstiefe).
KNOWN_SCOPES = (SCOPE_CORE, SCOPE_SUPERVISOR, SCOPE_HOST)

# Umfaenge, die es ohne Supervisor gar nicht geben kann.
SCOPES_NEEDING_SUPERVISOR = frozenset({SCOPE_SUPERVISOR, SCOPE_HOST})

# Ohne diese Integration gibt es keinen Supervisor — also auch kein hassio.host_reboot.
SUPERVISOR_COMPONENT = "hassio"

# Supervisor-API: Neustart der Verwaltungsschicht. Auth ueber SUPERVISOR_TOKEN aus der Env.
SUPERVISOR_RESTART_URL = "http://supervisor/supervisor/restart"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"

# Der Supervisor beendet sich waehrend der Antwort — kurz warten, nicht ewig.
SUPERVISOR_TIMEOUT_SECONDS = 30

# Fehlertext defensiv kuerzen (Backend-Spalte + Log nicht aufblaehen).
MAX_ERROR_LEN = 500


class RestartHandler:
    """Verarbeitet die ``restart``-Poll-Aktion (#127)."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        backend_url: str,
        api_key: str,
    ) -> None:
        self._hass = hass
        self._session = session
        self._endpoint = backend_url.rstrip("/") + "/api/agent/restart-ack"
        self._api_key = api_key

    async def handle(self, data: dict[str, Any]) -> None:
        """Poll-Handler fuer ``action == "restart"``.

        Einziges Nutzdatum ist ``scope`` (#144) — siehe Modul-Docstring.
        """
        scope = str((data or {}).get("scope") or SCOPE_CORE).lower()
        if scope not in KNOWN_SCOPES:
            scope = SCOPE_CORE

        # Supervisor- und Geraete-Neustart ohne Supervisor koennen nicht klappen. Lieber sofort
        # mit einer nennbaren Ursache scheitern, als den Integrator zehn Minuten warten zu lassen.
        if (
            scope in SCOPES_NEEDING_SUPERVISOR
            and SUPERVISOR_COMPONENT not in self._hass.config.components
        ):
            _LOGGER.warning(
                "Neustart (scope=%s) angefordert, aber kein Supervisor vorhanden (%s fehlt)",
                scope,
                SUPERVISOR_COMPONENT,
            )
            await self._ack(
                ACK_FAILED,
                "Kein Supervisor vorhanden: diesen Neustart gibt es nur auf "
                "Home Assistant OS oder Supervised.",
            )
            return

        if scope == SCOPE_SUPERVISOR:
            await self._restart_supervisor()
            return

        domain, service = SERVICE_BY_SCOPE[scope]
        _LOGGER.warning(
            "Neustart auf Backend-Befehl (scope=%s): rufe %s.%s auf", scope, domain, service
        )

        # Erst quittieren, dann neu starten: nach dem Service-Call ist der Prozess weg.
        await self._ack(ACK_RESTARTING)
        try:
            await self._hass.services.async_call(domain, service, {}, blocking=False)
        except Exception as err:  # noqa: BLE001 — ein Service-Fehler darf den Poll nicht crashen
            _LOGGER.warning("%s.%s fehlgeschlagen: %s", domain, service, err)
            await self._ack(ACK_FAILED, str(err))

    async def _restart_supervisor(self) -> None:
        """Startet die Verwaltungsschicht ueber die Supervisor-API neu (#144).

        Der Agent ueberlebt diesen Neustart — also erst ausfuehren, dann das Ergebnis melden.
        Nur ein 2xx gilt als Erfolg; alles andere geht als ``failed`` samt Statuscode raus,
        statt als scheinbarer Erfolg durchzugehen.
        """
        token = os.environ.get(SUPERVISOR_TOKEN_ENV)
        if not token:
            _LOGGER.warning(
                "Supervisor-Neustart angefordert, aber kein %s in der Umgebung",
                SUPERVISOR_TOKEN_ENV,
            )
            await self._ack(
                ACK_FAILED,
                "Kein Supervisor-Zugang: der Agent laeuft ohne SUPERVISOR_TOKEN und kann die "
                "Supervisor-API nicht aufrufen.",
            )
            return

        _LOGGER.warning(
            "Neustart auf Backend-Befehl (scope=supervisor): rufe %s auf",
            SUPERVISOR_RESTART_URL,
        )
        timeout = aiohttp.ClientTimeout(total=SUPERVISOR_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                SUPERVISOR_RESTART_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            ) as resp:
                if not 200 <= resp.status < 300:
                    body = (await resp.text())[:200]
                    _LOGGER.warning(
                        "Supervisor-Neustart fehlgeschlagen (HTTP %d): %s", resp.status, body
                    )
                    await self._ack(
                        ACK_FAILED,
                        f"Supervisor-API antwortete mit HTTP {resp.status}: {body}",
                    )
                    return
        except Exception as err:  # noqa: BLE001 — ein Netzwerkfehler darf den Poll nicht crashen
            _LOGGER.warning("Supervisor-Neustart fehlgeschlagen: %s", err)
            await self._ack(ACK_FAILED, str(err))
            return

        await self._ack(ACK_RESTARTING)

    async def _ack(self, status: str, error: str | None = None) -> None:
        """Quittiert den Neustart ans Backend (``POST /api/agent/restart-ack``).

        Serverseitig idempotent und ohne Wirkung, wenn kein Neustart laeuft. Eine
        gescheiterte Quittung wird nur geloggt — der Neustart selbst haengt nicht
        daran, und die Bestaetigung kommt ohnehin ueber den State-Push.
        """
        body: dict[str, Any] = {"status": status}
        if error:
            body["error"] = error[:MAX_ERROR_LEN]
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._session.post(
                self._endpoint, json=body, headers=headers, timeout=timeout
            ) as resp:
                if 200 <= resp.status < 300:
                    _LOGGER.debug("Neustart quittiert (status=%s, HTTP %d)", status, resp.status)
                else:
                    _LOGGER.warning(
                        "Neustart-Quittung '%s' fehlgeschlagen (HTTP %d)", status, resp.status
                    )
        except Exception as err:  # noqa: BLE001 — Quittungsfehler duerfen nichts crashen
            _LOGGER.warning("Neustart-Quittung '%s' Netzwerkfehler: %s", status, err)
