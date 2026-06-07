"""RequestPoller — pollt alle 15 s das Backend auf eingehende Aktionen (TODO #50.20).

GET {backend_url}/api/agent/poll mit X-API-Key-Header.

Antwort-Semantik:
  204 No Content  → nichts zu tun
  200 OK          → JSON mit "action"-Feld; Dispatch an registrierte Handler

Bekannte Actions:
  "connection_request"  → RemoteAccessManager._on_connection_request(data)
  "connection_accepted" → TunnelManager._on_connection_accepted(data)

Timeout: 10 s. Alle Fehler werden nur geloggt, Poller läuft weiter.
Transiente Gateway-Fehler (502/503/504) gehen auf DEBUG (selbstheilend),
alle anderen unerwarteten Stati auf WARNING.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import POLL_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

ActionHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RequestPoller:
    """Pollt periodisch den /api/agent/poll-Endpoint und dispatcht Aktionen."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        backend_url: str,
        api_key: str,
    ) -> None:
        self._hass = hass
        self._session = session
        self._endpoint = backend_url.rstrip("/") + "/api/agent/poll"
        self._api_key = api_key
        self._unsub_interval = None
        # Reentrancy-Guard (#108): der reguläre 15-s-Tick und der Reconnect-Loop
        # rufen beide _poll_once. Zwei parallele Polls würden zwei Tunnel-Token
        # ausgeben — der zweite expired den ersten (expireExisting), bevor dessen
        # WS-Aufbau fertig ist → der erste Tunnel-Connect scheitert mit 4001.
        # Darum läuft immer nur ein Poll zur Zeit.
        self._polling = False

        # Handler-Registry: action-Name → async Callable
        self._handlers: dict[str, ActionHandler] = {}

    def register_handler(self, action: str, handler: ActionHandler) -> None:
        """Registriert einen Handler für eine bestimmte action."""
        self._handlers[action] = handler

    def start(self) -> None:
        """Startet den Polling-Timer. Erster Poll direkt nach dem Intervall."""
        self._unsub_interval = async_track_time_interval(
            self._hass,
            self._tick,
            datetime.timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        # Ersten Poll direkt starten — nicht 15 s warten
        self._hass.async_create_task(self._poll_once())

    def stop(self) -> None:
        """Stoppt den Polling-Timer."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    # --------------------------------------------------------- Intervall

    async def _tick(self, _now: Any) -> None:
        await self._poll_once()

    async def _poll_once(self) -> None:
        """Führt einen einzelnen Poll durch — reentrancy-geschützt (#108).

        Läuft bereits ein Poll (z.B. regulärer Tick), wird ein zweiter (z.B. vom
        Reconnect-Loop) übersprungen, damit nicht zwei Tunnel-Token parallel
        ausgegeben werden (der zweite würde den ersten via expireExisting entwerten)."""
        if self._polling:
            _LOGGER.debug("Poll läuft bereits — paralleler Poll übersprungen")
            return
        self._polling = True
        try:
            await self._do_poll()
        finally:
            self._polling = False

    async def _do_poll(self) -> None:
        """Eigentliche Poll-Logik — nur über _poll_once aufrufen (Reentrancy-Guard)."""
        headers = {"X-API-Key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with self._session.get(
                self._endpoint, headers=headers, timeout=timeout
            ) as resp:
                if resp.status == 204:
                    # Keine ausstehende Backend-Aktion. Wir dispatchen dennoch eine
                    # synthetische "idle"-Aktion, damit Handler verwaiste Zustaende
                    # aufraeumen koennen — z.B. das Repair-Issue einer abgebrochenen
                    # oder abgelaufenen Verbindungsanfrage (#90).
                    await self._dispatch({"action": "idle"})
                    return

                if resp.status == 200:
                    try:
                        data: dict[str, Any] = await resp.json()
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning(
                            "Poll-Antwort 200 enthielt kein gültiges JSON — ignoriert"
                        )
                        return
                    await self._dispatch(data)
                    return

                if resp.status in (401, 403):
                    _LOGGER.warning(
                        "Poll abgelehnt (HTTP %d) — API-Key prüfen", resp.status
                    )
                    return

                if resp.status in (502, 503, 504):
                    # Transiente Gateway-/Proxy-Fehler: Backend kurz nicht
                    # erreichbar (Deploy, Neustart, Idle-Hickup). Selbstheilend —
                    # der nächste Poll-Tick versucht es erneut. DEBUG statt
                    # WARNING, sonst Log-Rauschen bei jedem Backend-Deploy.
                    _LOGGER.debug(
                        "Poll erhielt transienten Gateway-Status HTTP %d — ignoriert",
                        resp.status,
                    )
                    return

                _LOGGER.warning(
                    "Unerwarteter Poll-Status HTTP %d — ignoriert", resp.status
                )

        except asyncio.TimeoutError:
            _LOGGER.warning("Poll-Request Timeout (>10 s)")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Poll-Netzwerkfehler: %s", err)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        """Ruft den zum action-Feld passenden Handler auf."""
        action: str = data.get("action") or ""
        if not action:
            _LOGGER.warning("Poll-Antwort ohne 'action'-Feld — ignoriert: %s", data)
            return

        handler = self._handlers.get(action)
        if handler is None:
            _LOGGER.debug("Kein Handler für Poll-Aktion '%s'", action)
            return

        _LOGGER.debug("Poll-Aktion '%s' dispatcht", action)
        try:
            await handler(data)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Handler für Poll-Aktion '%s' ist ausgefallen", action)
