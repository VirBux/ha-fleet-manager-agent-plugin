"""Tests fuer den UpdateCommandHandler (update_batch-Poll-Aktion, #103)."""

from __future__ import annotations

from typing import Any

import pytest

from ha_fleet_agent.update_handler import UpdateCommandHandler


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
    def __init__(self, fail_for: set[str] | None = None):
        self.services = FakeServices(fail_for)


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


def _report_by_command(session: FakeSession) -> dict[str, dict]:
    """Mappt commandId -> Report-Body (URL-Schema .../update-commands/<id>/report)."""
    return {p["url"].split("/")[-2]: p["json"] for p in session.posts}


# --------------------------------------------------------- Tests


@pytest.mark.asyncio
async def test_einzelner_command_loest_install_aus_und_meldet_started():
    """Ein Command: update.install mit entity_id + Report {status: started}."""
    hass = FakeHass()
    session = FakeSession()
    await _handler(hass, session).handle(
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
    assert call["blocking"] is False  # nicht-blockierend (Research §6)

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
    await _handler(hass, session).handle(
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
    await _handler(hass, session).handle(
        {"commands": [{"commandId": "c1", "entity_id": "update.x", "backup": False}]}
    )
    assert "backup" not in hass.services.calls[0]["data"]


@pytest.mark.asyncio
async def test_sequenziell_und_fehler_bricht_kette_nicht():
    """Ein fehlschlagendes update.install meldet 'failed', stoppt aber die Kette nicht."""
    hass = FakeHass(fail_for={"update.boom"})
    session = FakeSession()
    await _handler(hass, session).handle(
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
    await _handler(hass, session).handle(
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
    await handler.handle({"action": "update_batch"})
    await handler.handle({"commands": []})
    await handler.handle({"commands": "kein-array"})
    assert hass.services.calls == []
    assert session.posts == []


@pytest.mark.asyncio
async def test_command_ohne_id_oder_entity_uebersprungen():
    """Commands ohne commandId oder entity_id werden uebersprungen (kein Install)."""
    hass = FakeHass()
    session = FakeSession()
    await _handler(hass, session).handle(
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
        def post(self, *a: Any, **kw: Any):
            class _Ctx:
                async def __aenter__(self):
                    raise RuntimeError("net down")

                async def __aexit__(self, *_):
                    return False

            return _Ctx()

    hass = FakeHass()
    handler = UpdateCommandHandler(
        hass, _BoomSession(), "https://api.ha-fleet-manager.com", "key"
    )
    await handler.handle(
        {"commands": [{"commandId": "c1", "entity_id": "update.x"}]}
    )
    assert len(hass.services.calls) == 1  # Install lief, Report-Fehler abgefangen
