"""Tests fuer den TunnelReconnector (#108 Phase C)."""

from __future__ import annotations

import asyncio

import pytest

from ha_fleet_agent import reconnect as reconnect_module
from ha_fleet_agent.reconnect import TunnelReconnector


class FakeHass:
    def __init__(self):
        self._tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name=None):
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Backoff-Delays auf 0 — die Tests sollen nicht real warten."""
    monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_DELAY_SECONDS", 0)


async def _drain(hass: FakeHass) -> None:
    await asyncio.gather(*hass._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_trigger_uebersprungen_ohne_offene_session():
    """Ohne offene Wartungs-Session wird kein Reconnect-Loop gestartet."""
    hass = FakeHass()
    polls: list[int] = []

    async def poll_once():
        polls.append(1)

    rc = TunnelReconnector(
        hass,
        poll_once=poll_once,
        is_tunnel_up=lambda: False,
        is_session_open=lambda: False,
    )
    rc.trigger()
    await _drain(hass)

    assert polls == []


@pytest.mark.asyncio
async def test_reconnect_erfolgreich_beim_ersten_poll():
    """Poll baut den Tunnel auf → Loop endet sofort, kein give_up."""
    hass = FakeHass()
    tunnel_up = {"v": False}
    gave_up: list[int] = []

    async def poll_once():
        tunnel_up["v"] = True  # connection_accepted-Handler baut Tunnel auf

    async def on_give_up():
        gave_up.append(1)

    rc = TunnelReconnector(
        hass,
        poll_once=poll_once,
        is_tunnel_up=lambda: tunnel_up["v"],
        is_session_open=lambda: True,
        on_give_up=on_give_up,
    )
    rc.trigger()
    await _drain(hass)

    assert tunnel_up["v"] is True
    assert gave_up == []  # nicht aufgegeben


@pytest.mark.asyncio
async def test_reconnect_gibt_nach_max_versuchen_auf_und_beendet_session(monkeypatch):
    """Tunnel kommt nie hoch → nach MAX_ATTEMPTS Polls greift on_give_up."""
    monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_ATTEMPTS", 3)
    hass = FakeHass()
    polls: list[int] = []
    gave_up: list[int] = []

    async def poll_once():
        polls.append(1)

    async def on_give_up():
        gave_up.append(1)

    rc = TunnelReconnector(
        hass,
        poll_once=poll_once,
        is_tunnel_up=lambda: False,
        is_session_open=lambda: True,
        on_give_up=on_give_up,
    )
    rc.trigger()
    await _drain(hass)

    assert len(polls) == 3
    assert gave_up == [1]


@pytest.mark.asyncio
async def test_reconnect_bricht_ab_wenn_session_schliesst(monkeypatch):
    """Schliesst die Session waehrend des Loops, wird abgebrochen (kein give_up)."""
    monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_ATTEMPTS", 5)
    hass = FakeHass()
    session_open = {"v": True}
    polls: list[int] = []
    gave_up: list[int] = []

    async def poll_once():
        polls.append(1)
        if len(polls) >= 2:
            session_open["v"] = False  # Session endet nach 2 Polls

    async def on_give_up():
        gave_up.append(1)

    rc = TunnelReconnector(
        hass,
        poll_once=poll_once,
        is_tunnel_up=lambda: False,
        is_session_open=lambda: session_open["v"],
        on_give_up=on_give_up,
    )
    rc.trigger()
    await _drain(hass)

    # Loop bricht ab, sobald die Session zu ist — kein give_up (Session schon weg).
    assert len(polls) <= 3
    assert gave_up == []


@pytest.mark.asyncio
async def test_trigger_idempotent_kein_zweiter_loop(monkeypatch):
    """Zweiter trigger waehrend ein Loop laeuft startet keinen zweiten Loop."""
    monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_ATTEMPTS", 3)
    hass = FakeHass()
    polls: list[int] = []
    gate = asyncio.Event()

    async def poll_once():
        polls.append(1)
        await gate.wait()  # Loop im ersten Poll anhalten

    rc = TunnelReconnector(
        hass,
        poll_once=poll_once,
        is_tunnel_up=lambda: False,
        is_session_open=lambda: True,
    )
    rc.trigger()
    await asyncio.sleep(0)  # ersten Loop anlaufen lassen
    rc.trigger()  # zweiter Versuch — darf keinen zweiten Loop starten
    await asyncio.sleep(0)

    assert len(hass._tasks) == 1
    gate.set()
    rc.cancel()
    await _drain(hass)


@pytest.mark.asyncio
async def test_cancel_stoppt_laufenden_loop():
    hass = FakeHass()
    gate = asyncio.Event()

    async def poll_once():
        await gate.wait()

    rc = TunnelReconnector(
        hass,
        poll_once=poll_once,
        is_tunnel_up=lambda: False,
        is_session_open=lambda: True,
    )
    rc.trigger()
    await asyncio.sleep(0)
    rc.cancel()
    await _drain(hass)

    assert rc._task is None
