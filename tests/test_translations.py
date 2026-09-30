"""Tests für die Zustandsübersetzungen der ENUM-Sensoren.

Ein Sensor mit festen Werten ist als ENUM mit ``_attr_options`` deklariert, und
jede Option braucht einen Eintrag unter
``entity.sensor.<translation_key>.state.<option>``. Fehlt er, erscheint im UI
der rohe Wert (etwa ``idle``).

``sensor.py`` lässt sich ohne Sensor-Stubs nicht importieren, deshalb liest der
Test die Klassen per AST aus dem Quelltext. Er arbeitet bewusst fail-closed:
Jede Klasse, die – selbst oder über eine Basisklasse aus ``sensor.py`` – eine
ENUM-device_class setzt (``<Alias>.ENUM`` oder ``"enum"``), muss
``_attr_options`` als Klassenattribut liefern (``=`` oder annotiert) und
``_attr_translation_key`` ebenso. Unvollständig darf nur eine Klasse sein, die
als Basis einer anderen dient. ``_attr_options`` ohne ENUM ist ebenfalls ein
Fehler. Werte dürfen Literale oder Konstanten aus ``const.py``
sein. Alles, was sich so nicht auflösen lässt, und jede Zuweisung
``self._attr_*`` der geprüften Attribute in einer Methode lässt den Test
fehlschlagen, statt den Sensor stillschweigend zu überspringen.
"""

from __future__ import annotations

import ast
import json
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from ha_fleet_agent import const
from ha_fleet_agent.const import (
    STATUS_IDLE,
    STATUS_PRE_AUTHORIZED,
    STATUS_SESSION_ACTIVE,
    SUPPORTED_LANGUAGES,
)

_COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "ha_fleet_agent"
)
_GEPRUEFTE_ATTRIBUTE = ("_attr_device_class", "_attr_options", "_attr_translation_key")


def _ist_enum(device_class: object) -> bool:
    # Erkennt SensorDeviceClass.ENUM auch unter Import-Alias und den Rohwert "enum".
    return isinstance(device_class, str) and (
        device_class == "enum" or device_class.endswith(".ENUM")
    )


def _wert(node: ast.expr) -> object:
    """Löst Literale, Konstanten aus ``const.py`` und ``SensorDeviceClass.X`` auf."""
    if isinstance(node, ast.Name):
        if not hasattr(const, node.id):
            raise AssertionError(
                f"sensor.py: {node.id} ist weder Literal noch Konstante aus const.py"
            )
        return getattr(const, node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_wert(elt) for elt in node.elts]
    if isinstance(node, ast.Attribute):
        return ast.unparse(node)
    try:
        return ast.literal_eval(node)
    except ValueError as err:
        raise AssertionError(
            f"sensor.py: {ast.unparse(node)} lässt sich nicht auflösen"
        ) from err


def _klassenattribute(klasse: ast.ClassDef) -> dict[str, object]:
    attrs: dict[str, object] = {}
    for stmt in klasse.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            ziel, wert = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            ziel, wert = stmt.target, stmt.value
        else:
            continue
        if isinstance(ziel, ast.Name) and ziel.id in _GEPRUEFTE_ATTRIBUTE:
            attrs[ziel.id] = _wert(wert)
    return attrs


def _pruefe_keine_instanzzuweisung(klasse: ast.ClassDef) -> None:
    for node in ast.walk(klasse):
        ziele = []
        if isinstance(node, ast.Assign):
            ziele = node.targets
        elif isinstance(node, ast.AnnAssign):
            ziele = [node.target]
        for ziel in ziele:
            if (
                isinstance(ziel, ast.Attribute)
                and isinstance(ziel.value, ast.Name)
                and ziel.value.id == "self"
                and ziel.attr in _GEPRUEFTE_ATTRIBUTE
            ):
                raise AssertionError(
                    f"sensor.py: {klasse.name} setzt self.{ziel.attr} in einer Methode – "
                    "bitte als Klassenattribut deklarieren, damit der Test es sieht"
                )


@lru_cache(maxsize=1)
def _enum_sensoren() -> dict[str, tuple[str, ...]]:
    """translation_key -> Optionen für jede ENUM-Sensor-Klasse in ``sensor.py``."""
    tree = ast.parse((_COMPONENT_DIR / "sensor.py").read_text(encoding="utf-8"))
    klassen = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    basen = {
        b.id for k in klassen.values() for b in k.bases if isinstance(b, ast.Name)
    }

    def effektiv(name: str) -> dict[str, object]:
        klasse = klassen[name]
        attrs: dict[str, object] = {}
        # Rückwärts, damit wie in der MRO die zuerst aufgeführte Basis gewinnt.
        for basis in reversed(klasse.bases):
            if isinstance(basis, ast.Name) and basis.id in klassen:
                attrs.update(effektiv(basis.id))
        attrs.update(_klassenattribute(klasse))
        return attrs

    result: dict[str, tuple[str, ...]] = {}
    for name, klasse in klassen.items():
        _pruefe_keine_instanzzuweisung(klasse)
        attrs = effektiv(name)
        ist_enum = _ist_enum(attrs.get("_attr_device_class"))
        vollstaendig = "_attr_options" in attrs and "_attr_translation_key" in attrs
        # Eine Basis darf unvollständig sein, ihre Unterklassen werden geprüft.
        # Ist sie selbst ein vollständiger ENUM-Sensor, wird sie mitgeprüft.
        if name in basen and not (ist_enum and vollstaendig):
            continue
        if not ist_enum:
            assert "_attr_options" not in attrs, (
                f"sensor.py: {name} hat _attr_options, aber keine ENUM-device_class"
            )
            continue
        assert "_attr_options" in attrs, (
            f"sensor.py: ENUM-Sensor {name} ohne _attr_options"
        )
        assert "_attr_translation_key" in attrs, (
            f"sensor.py: ENUM-Sensor {name} ohne _attr_translation_key"
        )
        result[attrs["_attr_translation_key"]] = tuple(attrs["_attr_options"])
    return result


def _dateien() -> list[Path]:
    return [_COMPONENT_DIR / "strings.json"] + [
        _COMPONENT_DIR / "translations" / f"{lang}.json" for lang in SUPPORTED_LANGUAGES
    ]


def test_verbindungs_und_fernzugriffs_status_sind_enum_sensoren():
    sensoren = _enum_sensoren()
    assert sensoren["connection_state"] == ("connected", "disconnected")
    assert sensoren["remote_access_status"] == (
        STATUS_IDLE,
        STATUS_PRE_AUTHORIZED,
        STATUS_SESSION_ACTIVE,
    )


@pytest.mark.parametrize("path", _dateien(), ids=lambda p: p.name)
def test_jede_enum_option_ist_uebersetzt(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, options in _enum_sensoren().items():
        states = data["entity"]["sensor"][key].get("state", {})
        assert set(states) == set(options), (
            f"{path.name}: entity.sensor.{key}.state passt nicht zu den Optionen"
        )
        assert all(str(v).strip() for v in states.values()), (
            f"{path.name}: leere Übersetzung in entity.sensor.{key}.state"
        )


@pytest.fixture
def sensor_quelltext(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Lässt ``_enum_sensoren()`` einen eigenen ``sensor.py``-Quelltext lesen."""

    def schreiben(quelltext: str) -> None:
        (tmp_path / "sensor.py").write_text(quelltext, encoding="utf-8")
        _enum_sensoren.cache_clear()

    monkeypatch.setattr(sys.modules[__name__], "_COMPONENT_DIR", tmp_path)
    yield schreiben
    _enum_sensoren.cache_clear()


_ENUM_KOPF = "class A:\n    _attr_device_class = SensorDeviceClass.ENUM\n"


@pytest.mark.parametrize(
    "quelltext,meldung",
    [
        (
            _ENUM_KOPF + "    _attr_translation_key = 'a'\n    _attr_options = [GIBT_ES_NICHT]\n",
            "weder Literal noch Konstante",
        ),
        (_ENUM_KOPF + "    _attr_translation_key = 'a'\n", "ohne _attr_options"),
        (_ENUM_KOPF + "    _attr_options = ['x']\n", "ohne _attr_translation_key"),
        (
            # Optionen stehen auch im Klassenkörper, damit nur die Methoden-Prüfung greift.
            _ENUM_KOPF + "    _attr_translation_key = 'a'\n    _attr_options = ['x']\n"
            "    def __init__(self):\n        self._attr_options = ['y']\n",
            "in einer Methode",
        ),
        (
            "class A:\n    _attr_translation_key = 'a'\n    _attr_options = ['x']\n",
            "keine ENUM-device_class",
        ),
    ],
    ids=[
        "unbekannte_konstante",
        "enum_ohne_optionen",
        "konkreter_enum_ohne_key",
        "optionen_in_methode",
        "optionen_ohne_enum",
    ],
)
def test_nicht_erkennbarer_enum_sensor_laesst_test_fehlschlagen(
    sensor_quelltext, quelltext: str, meldung: str
):
    sensor_quelltext(quelltext)
    with pytest.raises(AssertionError, match=meldung):
        _enum_sensoren()


def test_geerbte_und_annotierte_attribute_werden_erkannt(sensor_quelltext):
    sensor_quelltext(
        "class Basis:\n    _attr_device_class = SensorDeviceClass.ENUM\n"
        "    _attr_options: list[str] = [STATUS_IDLE]\n"
        "class Kind(Basis):\n    _attr_translation_key = 'kind'\n"
    )
    assert _enum_sensoren() == {"kind": (STATUS_IDLE,)}


def test_abstrakte_basis_darf_optionen_den_unterklassen_ueberlassen(sensor_quelltext):
    sensor_quelltext(
        "class Basis:\n    _attr_device_class = SensorDeviceClass.ENUM\n"
        "class Kind(Basis):\n    _attr_translation_key = 'kind'\n"
        "    _attr_options = ['x']\n"
    )
    assert _enum_sensoren() == {"kind": ("x",)}


def test_konkreter_sensor_als_basis_wird_selbst_geprueft(sensor_quelltext):
    sensor_quelltext(
        _ENUM_KOPF + "    _attr_translation_key = 'a'\n    _attr_options = ['x']\n"
        "class B(A):\n    _attr_translation_key = 'b'\n"
    )
    assert _enum_sensoren() == {"a": ("x",), "b": ("x",)}


@pytest.mark.parametrize("device_class", ["SDC.ENUM", "'enum'"])
def test_enum_wird_auch_mit_alias_oder_rohwert_erkannt(sensor_quelltext, device_class):
    sensor_quelltext(
        f"class A:\n    _attr_device_class = {device_class}\n"
        "    _attr_translation_key = 'a'\n    _attr_options = ['x']\n"
    )
    assert _enum_sensoren() == {"a": ("x",)}


def test_erste_basis_gewinnt_wie_in_der_mro(sensor_quelltext):
    sensor_quelltext(
        "class B1:\n    _attr_device_class = SensorDeviceClass.ENUM\n"
        "    _attr_options = ['erste']\n"
        "class B2:\n    _attr_device_class = SensorDeviceClass.ENUM\n"
        "    _attr_options = ['zweite']\n"
        "class C(B1, B2):\n    _attr_translation_key = 'c'\n"
    )
    assert _enum_sensoren() == {"c": ("erste",)}
