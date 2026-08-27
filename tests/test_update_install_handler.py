"""Tests fuer den UpdateCommandHandler (update_batch-Poll-Aktion, #103)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ha_fleet_agent import update_handler as update_handler_module
from ha_fleet_agent.update_handler import UpdateCommandHandler


@pytest.fixture(autouse=True)
def _kein_report_backoff(monkeypatch):
    """Backoff der Report-Wiederholung (#142) im Test auf 0 setzen.

    Die Anzahl der Versuche bleibt unveraendert — nur die Pausen dazwischen fallen weg,
    sonst kostete jeder Netzwerkfehler-Test die echten 6 Sekunden Wartezeit.
    """
    monkeypatch.setattr(
        update_handler_module, "REPORT_BACKOFF_SECONDS", (0, 0), raising=True
    )


# --------------------------------------------------------- Stubs


class FakeServices:
    """hass.services-Stub: zeichnet async_call-Aufrufe auf, kann gezielt werfen."""

    def __init__(self, fail_for: set[str] | None = None):
        self.calls: list[dict] = []
        self._fail_for = fail_for or set()

    async def async_call(
        self, domain, service, service_data=None, blocking=False
    ):  # noqa: ANN001
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "data": dict(service_data or {}),
                "blocking": blocking,
            }
        )
        entity_id = (service_data or {}).get("entity_id")
        if entity_id in self._fail_for:
            raise RuntimeError(f"install failed for {entity_id}")


class FakeHass:
    """hass-Stub. ``async_create_task`` fuehrt den Batch als echten Task aus — der
    Handler kehrt sofort zurueck, die Tests warten ueber :meth:`settle` auf ihn."""

    def __init__(self, fail_for: set[str] | None = None):
        self.services = FakeServices(fail_for)
        self.tasks: list[asyncio.Task] = []

    def async_create_task(self, coro, name: str | None = None):  # noqa: ANN001
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def settle(self) -> None:
        """Wartet, bis alle gestarteten Hintergrund-Tasks durch sind."""
        if self.tasks:
            await asyncio.gather(*self.tasks)


class _FakeResponse:
    def __init__(self, status: int = 204):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    """aiohttp.ClientSession-Stub — zeichnet Report-POSTs auf."""

    def __init__(self, status: int = 204):
        self._status = status
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._status)


def _handler(hass: FakeHass, session: FakeSession) -> UpdateCommandHandler:
    return UpdateCommandHandler(
        hass, session, "https://api.ha-fleet-manager.com", "secret-key"
    )


async def _run(handler: UpdateCommandHandler, hass: FakeHass, payload: dict) -> None:
    """handle() anstossen und den Hintergrund-Batch auslaufen lassen."""
    await handler.handle(payload)
    await hass.settle()


def _report_by_command(session: FakeSession) -> dict[str, dict]:
    """Mappt commandId -> Report-Body (URL-Schema .../update-commands/<id>/report)."""
    return {p["url"].split("/")[-2]: p["json"] for p in session.posts}


# --------------------------------------------------------- Tests


@pytest.mark.asyncio
async def test_einzelner_command_loest_install_aus_und_meldet_started():
    """Ein Command: update.install mit entity_id + Report {status: started}."""
    hass = FakeHass()
    session = FakeSession()
    await _run(_handler(hass, session), hass, 
        {
            "action": "update_batch",
            "commands": [
                {"commandId": "c1", "entity_id": "update.terminal_ssh_update"}
            ],
        }
    )

    assert len(hass.services.calls) == 1
    call = hass.services.calls[0]
    assert call["domain"] == "update"
    assert call["service"] == "install"
    assert call["data"] == {"entity_id": "update.terminal_ssh_update"}
    # blocking=True: nur so schlaegt ein fehlgeschlagenes update.install bis zu uns
    # durch. Den Poll-Tick haelt das nicht auf — der Batch laeuft im eigenen Task.
    assert call["blocking"] is True

    assert len(session.posts) == 1
    post = session.posts[0]
    assert (
        post["url"]
        == "https://api.ha-fleet-manager.com/api/agent/update-commands/c1/report"
    )
    assert post["json"] == {"status": "started"}
    assert post["headers"]["X-API-Key"] == "secret-key"


@pytest.mark.asyncio
async def test_version_und_backup_nur_wenn_gesetzt():
    """version/backup gehen nur mit, wenn gesetzt — sonst nur entity_id."""
    hass = FakeHass()
    session = FakeSession()
    await _run(_handler(hass, session), hass, 
        {
            "commands": [
                {
                    "commandId": "c1",
                    "entity_id": "update.home_assistant_core_update",
                    "version": "2026.5.4",
                    "backup": True,
                },
                {"commandId": "c2", "entity_id": "update.addon"},
            ]
        }
    )

    assert hass.services.calls[0]["data"] == {
        "entity_id": "update.home_assistant_core_update",
        "version": "2026.5.4",
        "backup": True,
    }
    # Keine Optionalfelder → kein version/backup im Service-Call.
    assert hass.services.calls[1]["data"] == {"entity_id": "update.addon"}


@pytest.mark.asyncio
async def test_backup_false_wird_nicht_mitgesendet():
    """backup=False ist der Default und soll nicht explizit mitgehen."""
    hass = FakeHass()
    session = FakeSession()
    await _run(_handler(hass, session), hass, 
        {"commands": [{"commandId": "c1", "entity_id": "update.x", "backup": False}]}
    )
    assert "backup" not in hass.services.calls[0]["data"]


@pytest.mark.asyncio
async def test_sequenziell_und_fehler_bricht_kette_nicht():
    """Ein fehlschlagendes update.install meldet 'failed', stoppt aber die Kette nicht."""
    hass = FakeHass(fail_for={"update.boom"})
    session = FakeSession()
    await _run(_handler(hass, session), hass, 
        {
            "commands": [
                {"commandId": "c1", "entity_id": "update.boom"},
                {"commandId": "c2", "entity_id": "update.ok"},
            ]
        }
    )

    # Beide Installs wurden in Reihenfolge versucht.
    assert [c["data"]["entity_id"] for c in hass.services.calls] == [
        "update.boom",
        "update.ok",
    ]
    reports = _report_by_command(session)
    assert reports["c1"]["status"] == "failed"
    assert "error" in reports["c1"]
    assert reports["c2"] == {"status": "started"}


@pytest.mark.asyncio
async def test_tolerant_camel_und_snake_case():
    """commandId/command_id und entityId/entity_id werden beide akzeptiert."""
    hass = FakeHass()
    session = FakeSession()
    await _run(_handler(hass, session), hass, 
        {
            "commands": [
                {"command_id": "c1", "entity_id": "update.a"},
                {"commandId": "c2", "entityId": "update.b"},
            ]
        }
    )
    assert [c["data"]["entity_id"] for c in hass.services.calls] == [
        "update.a",
        "update.b",
    ]
    urls = [p["url"] for p in session.posts]
    assert any("/c1/report" in u for u in urls)
    assert any("/c2/report" in u for u in urls)


@pytest.mark.asyncio
async def test_leere_oder_fehlende_commands_tut_nichts():
    """Kein/leeres/kaputtes commands-Feld → kein Install, kein Report, kein Crash."""
    hass = FakeHass()
    session = FakeSession()
    handler = _handler(hass, session)
    await _run(handler, hass, {"action": "update_batch"})
    await _run(handler, hass, {"commands": []})
    await _run(handler, hass, {"commands": "kein-array"})
    assert hass.services.calls == []
    assert session.posts == []


@pytest.mark.asyncio
async def test_command_ohne_id_oder_entity_uebersprungen():
    """Commands ohne commandId oder entity_id werden uebersprungen (kein Install)."""
    hass = FakeHass()
    session = FakeSession()
    await _run(_handler(hass, session), hass, 
        {
            "commands": [
                {"commandId": "c1"},  # keine entity_id
                {"entity_id": "update.x"},  # keine commandId
                {"commandId": "c2", "entity_id": "update.ok"},
            ]
        }
    )
    assert len(hass.services.calls) == 1
    assert hass.services.calls[0]["data"]["entity_id"] == "update.ok"
    assert len(session.posts) == 1


@pytest.mark.asyncio
async def test_report_netzwerkfehler_kein_crash():
    """Ein fehlschlagender Report-POST darf den Handler nicht crashen."""

    class _BoomSession:
        def __init__(self):
            self.attempts = 0

        def post(self, *a: Any, **kw: Any):
            self.attempts += 1

            class _Ctx:
                async def __aenter__(self):
                    raise RuntimeError("net down")

                async def __aexit__(self, *_):
                    return False

            return _Ctx()

    hass = FakeHass()
    session = _BoomSession()
    handler = UpdateCommandHandler(
        hass, session, "https://api.ha-fleet-manager.com", "key"
    )
    await _run(handler, hass, 
        {"commands": [{"commandId": "c1", "entity_id": "update.x"}]}
    )
    assert len(hass.services.calls) == 1  # Install lief, Report-Fehler abgefangen
    # Drei Versuche (#142, B7) — danach traegt die Selbstheilung ueber den State-Push.
    assert session.attempts == 3


@pytest.mark.asyncio
async def test_fehlschlag_korrigiert_das_gemeldete_started():
    """Erst „started", dann „failed" — ein still fehlschlagendes Update darf in der
    App nicht als „laeuft" stehen bleiben (blocking=True macht den Fehler sichtbar)."""
    hass = FakeHass(fail_for={"update.boom"})
    session = FakeSession()
    await _run(
        _handler(hass, session),
        hass,
        {"commands": [{"commandId": "c1", "entity_id": "update.boom"}]},
    )

    assert [p["json"]["status"] for p in session.posts] == ["started", "failed"]
    assert "install failed" in session.posts[1]["json"]["error"]


@pytest.mark.asyncio
async def test_abbruch_beim_ha_neustart_meldet_keinen_fehlschlag():
    """Bricht der Task ab, weil HA herunterfaehrt (Core/OS-Update), ist das kein
    Fehlschlag — der Abschluss klaert der State-Push nach dem Neustart."""

    class _ShutdownServices(FakeServices):
        async def async_call(self, domain, service, service_data=None, blocking=False):  # noqa: ANN001
            await super().async_call(domain, service, service_data, blocking)
            raise asyncio.CancelledError

    hass = FakeHass()
    hass.services = _ShutdownServices()
    session = FakeSession()
    with pytest.raises(asyncio.CancelledError):
        await _run(
            _handler(hass, session),
            hass,
            {"commands": [{"commandId": "c1", "entity_id": "update.core"}]},
        )

    assert [p["json"]["status"] for p in session.posts] == ["started"]


# --------------------------------------------------------- #142: Retry + Idempotenz


@pytest.mark.asyncio
async def test_report_wird_nach_5xx_wiederholt_und_gibt_bei_erfolg_auf():
    """Ein 503 ist voruebergehend — der zweite Versuch quittiert, danach ist Schluss."""

    class _FlakySession:
        def __init__(self):
            self.posts: list[dict] = []

        def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
            self.posts.append({"url": url, "json": json})
            # Erster Versuch 503, danach 204.
            return _FakeResponse(503 if len(self.posts) == 1 else 204)

    hass = FakeHass()
    session = _FlakySession()
    handler = UpdateCommandHandler(
        hass, session, "https://api.ha-fleet-manager.com", "key"
    )
    await _run(handler, hass, {"commands": [{"commandId": "c1", "entity_id": "update.x"}]})

    assert len(session.posts) == 2


@pytest.mark.asyncio
async def test_report_wird_bei_4xx_nicht_wiederholt():
    """Ein 404 ist eine Aussage des Backends (Command unbekannt) — kein zweiter Anlauf."""
    hass = FakeHass()
    session = FakeSession(status=404)
    await _run(_handler(hass, session), hass, {"commands": [{"commandId": "c1", "entity_id": "update.x"}]})

    assert len(session.posts) == 1


@pytest.mark.asyncio
async def test_erneut_ausgelieferter_command_installiert_nicht_zweimal():
    """Idempotenz-Riegel (#142, B3): Nach einem verlorenen Report liefert der Watchdog
    denselben Command erneut aus — er wird dann nur quittiert, nicht neu installiert."""
    hass = FakeHass()
    session = FakeSession()
    handler = _handler(hass, session)
    batch = {"commands": [{"commandId": "c1", "entity_id": "update.x"}]}

    await _run(handler, hass, batch)
    hass.tasks.clear()
    await _run(handler, hass, batch)

    assert len(hass.services.calls) == 1  # genau ein update.install
    assert [p["json"]["status"] for p in session.posts] == ["started", "started"]


@pytest.mark.asyncio
async def test_riegel_gilt_je_command_nicht_je_entity():
    """Ein neuer Befehl fuer dieselbe Entity muss laufen — der Riegel merkt sich
    commandIds, nicht Entities (sonst liesse sich ein Update nie wiederholen)."""
    hass = FakeHass()
    session = FakeSession()
    handler = _handler(hass, session)

    await _run(handler, hass, {"commands": [{"commandId": "c1", "entity_id": "update.x"}]})
    hass.tasks.clear()
    await _run(handler, hass, {"commands": [{"commandId": "c2", "entity_id": "update.x"}]})

    assert len(hass.services.calls) == 2


@pytest.mark.asyncio
async def test_riegel_verfaellt_nach_ttl():
    """Der Riegel traegt nur ueber das Re-Dispatch-Fenster hinaus — danach wird
    aufgeraeumt, damit die Merkliste nicht unbegrenzt waechst."""
    hass = FakeHass()
    session = FakeSession()
    handler = _handler(hass, session)

    await _run(handler, hass, {"commands": [{"commandId": "c1", "entity_id": "update.x"}]})
    assert "c1" in handler._executed
    # Eintrag kuenstlich altern lassen (aelter als EXECUTED_TTL_SECONDS).
    handler._executed["c1"] -= update_handler_module.EXECUTED_TTL_SECONDS + 1
    hass.tasks.clear()

    await _run(handler, hass, {"commands": [{"commandId": "c1", "entity_id": "update.x"}]})

    assert len(hass.services.calls) == 2
