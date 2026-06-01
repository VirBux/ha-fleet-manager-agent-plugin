"""Tests für den ConnectionRequestRepairFlow.

Validiert nur die Plugin-Eigenlogik (Schritt-Übergänge, Aufrufe an
RemoteAccessManager.confirm_request). Die HA-Repair-Flow-Infrastruktur ist
durch Stubs in conftest abgebildet.
"""

from __future__ import annotations

import pytest

from ha_fleet_agent.repairs import ConnectionRequestRepairFlow


class _FakeManager:
    """Zeichnet confirm_request-Aufrufe auf."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def confirm_request(
        self,
        request_id: str,
        accepted: bool,
        duration_hours: int | None = None,
    ) -> None:
        self.calls.append(
            {
                "request_id": request_id,
                "accepted": accepted,
                "duration_hours": duration_hours,
            }
        )


def make_flow(manager: _FakeManager, **overrides) -> ConnectionRequestRepairFlow:
    defaults = {
        "request_id": "req-99",
        "subject": "Heizungssteuerung defekt",
        "reason": "Diagnose nötig",
        "requested_hours": 4,
    }
    defaults.update(overrides)
    return ConnectionRequestRepairFlow(manager=manager, **defaults)


# --------------------------------------------------------- init-Schritt


@pytest.mark.asyncio
async def test_init_zeigt_menu_mit_accept_und_reject():
    flow = make_flow(_FakeManager())
    result = await flow.async_step_init()

    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) == {"accept", "reject"}
    # Daten landen in placeholders → werden im Text gerendert
    placeholders = result["description_placeholders"]
    assert placeholders["subject"] == "Heizungssteuerung defekt"
    assert placeholders["reason"] == "Diagnose nötig"
    assert placeholders["requested_hours"] == "4"


# --------------------------------------------------------- accept-Schritt


@pytest.mark.asyncio
async def test_accept_ohne_input_zeigt_form_mit_default_duration():
    flow = make_flow(_FakeManager(), requested_hours=6)
    result = await flow.async_step_accept()

    assert result["type"] == "form"
    assert result["step_id"] == "accept"
    # Form-Schema ist vorhanden → Default = requested_hours
    assert result["data_schema"] is not None


@pytest.mark.asyncio
async def test_accept_mit_input_ruft_confirm_request_an():
    manager = _FakeManager()
    flow = make_flow(manager)

    result = await flow.async_step_accept({"duration_hours": 3})

    assert result["type"] == "create_entry"
    assert manager.calls == [
        {"request_id": "req-99", "accepted": True, "duration_hours": 3}
    ]


# --------------------------------------------------------- reject-Schritt


@pytest.mark.asyncio
async def test_reject_ruft_confirm_request_mit_false_auf():
    manager = _FakeManager()
    flow = make_flow(manager)

    result = await flow.async_step_reject()

    assert result["type"] == "create_entry"
    assert manager.calls == [
        {"request_id": "req-99", "accepted": False, "duration_hours": None}
    ]
