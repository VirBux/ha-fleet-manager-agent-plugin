"""Reconnect-Koordination nach unerwartetem Tunnel-Abriss (#108 Phase C).

Wenn der Tunnel wegbricht, OBWOHL die Wartungs-Session noch laeuft (Hauptfall:
geplanter Connector-Neustart), soll die Session von selbst zurueckkommen — ohne
dass der Integrator eine neue Verbindungsanfrage stellen muss.

Mechanik: Der ``TunnelForwarder`` ruft bei unerwartetem Close ``trigger()``.
Dieser startet einen Backoff-Loop, der wiederholt den regulaeren Agent-Poll
ausloest (``request_poller._poll_once``). Das Backend liefert dabei — solange
das Session-Fenster gueltig ist und kein Tunnel aktiv ist — ``connection_accepted``
mit einem frischen Token; der bestehende Phase-A-Handler baut den Tunnel dann
auf. Es gibt hier bewusst KEINEN eigenen Token-Abruf-Pfad.

Der Loop endet, sobald
  * der Tunnel wieder steht (``is_tunnel_up``),
  * die Wartungs-Session beendet ist (``is_session_open`` -> False), oder
  * das Versuchslimit erreicht ist (dann uebernimmt der regulaere 15-s-Poll).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from homeassistant.core import HomeAssistant

from .const import (
    RECONNECT_INITIAL_DELAY_SECONDS,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_MAX_DELAY_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class TunnelReconnector:
    """Stoesst nach unerwartetem Tunnel-Abriss einen Reconnect via Re-Poll an."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        poll_once: Callable[[], Awaitable[None]],
        is_tunnel_up: Callable[[], bool],
        is_session_open: Callable[[], bool],
        on_give_up: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._poll_once = poll_once
        self._is_tunnel_up = is_tunnel_up
        self._is_session_open = is_session_open
        # Aufgeruft, wenn alle Versuche erschoepft sind und kein Tunnel steht
        # (Connector dauerhaft tot ODER Integrator-Trennen/Ablauf → Backend liefert
        # nichts mehr): beendet die Wartungs-Session, damit sie nicht "offen" haengt.
        self._on_give_up = on_give_up
        self._task: asyncio.Task | None = None

    def trigger(self) -> None:
        """Startet einen Reconnect-Loop — idempotent (hoechstens ein Loop)."""
        if not self._is_session_open():
            _LOGGER.debug(
                "Reconnect uebersprungen — keine offene Wartungs-Session"
            )
            return
        if self._task is not None and not self._task.done():
            _LOGGER.debug("Reconnect laeuft bereits — kein zweiter Loop")
            return
        self._task = self._hass.async_create_background_task(
            self._run(), "ha_fleet_agent_tunnel_reconnect"
        )

    def cancel(self) -> None:
        """Bricht einen laufenden Reconnect-Loop ab (z.B. beim Plugin-Unload)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
            # Vor jedem Versuch die Abbruchbedingungen pruefen.
            if not self._is_session_open():
                _LOGGER.info("Reconnect abgebrochen — Wartungs-Session beendet")
                return
            if self._is_tunnel_up():
                _LOGGER.info("Reconnect: Tunnel steht wieder — fertig")
                return

            _LOGGER.info(
                "Reconnect-Versuch %d/%d (Tunnel weg, Session noch offen)",
                attempt,
                RECONNECT_MAX_ATTEMPTS,
            )
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Reconnect-Poll fehlgeschlagen", exc_info=True)

            # connection_accepted-Handler baut den Tunnel synchron im Poll auf —
            # direkt danach pruefen, ob er steht.
            if self._is_tunnel_up():
                _LOGGER.info("Reconnect erfolgreich nach Versuch %d", attempt)
                return

            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)

        if self._is_tunnel_up():
            return
        # Alle Versuche erschoepft, kein Tunnel: aufgeben und die Wartungs-Session
        # beenden (deckt Connector-dauerhaft-tot UND Integrator-Trennen/Ablauf ab,
        # wo das Backend keinen Reconnect-Token mehr liefert).
        _LOGGER.warning(
            "Reconnect nach %d Versuchen erfolglos — Wartungs-Session wird beendet",
            RECONNECT_MAX_ATTEMPTS,
        )
        if self._on_give_up is not None and self._is_session_open():
            try:
                await self._on_give_up()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("on_give_up-Callback fehlgeschlagen", exc_info=True)
