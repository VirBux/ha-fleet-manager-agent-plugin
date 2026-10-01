"""Sprache eines Config-Entries — gemeinsam für Dashboard, Remote-Zugriff und Backup (#211).

Lag bis Plugin 1.15.0 modulprivat in ``dashboard.py`` und wurde von dort importiert.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LANGUAGE, DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

_LOGGER = logging.getLogger(__name__)


def resolve_language(hass: HomeAssistant) -> str:
    """Liest die HA-Sprache und mappt sie auf eine unterstützte Plugin-Sprache.

    ``hass.config.language`` kann ``"de"``, ``"de_DE"``, ``"en"``, ``"en_GB"``,
    ``"es"``, ``"fr_FR"``, ``"hr"`` usw. sein — wir schneiden auf den
    2-Buchstaben-Präfix und behalten ihn, wenn er zu ``SUPPORTED_LANGUAGES``
    gehört; alles andere fällt auf ``DEFAULT_LANGUAGE`` (``"en"``).

    Wird seit Plugin 0.7.1 nur noch als **Fallback** verwendet: kanonische
    Quelle ist die im Config-Flow gewählte Sprache (``entry.data[CONF_LANGUAGE]``,
    siehe ``lang_from_entry``). HA-Sprache greift nur, wenn das Feld im
    ConfigEntry fehlt (0.7.0-Bestand).
    """
    raw = ""
    try:
        raw = getattr(hass.config, "language", "") or ""
    except Exception:  # noqa: BLE001
        _LOGGER.debug("hass.config.language nicht lesbar — nutze Default")
    short = raw[:2].lower() if isinstance(raw, str) else ""
    return short if short in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def lang_from_entry(entry: ConfigEntry, hass: HomeAssistant) -> str:
    """Liefert die Sprache für dieses ConfigEntry.

    Priorität:
    1. ``entry.data[CONF_LANGUAGE]`` — Endkunden-Wahl im Config-Flow
       (kanonische Quelle ab 0.7.1).
    2. ``resolve_language(hass)`` — Fallback für 0.7.0-Bestandsinstallationen,
       die das Feld noch nicht im ConfigEntry hatten.
    3. ``DEFAULT_LANGUAGE`` — letzter Fallback (sollte nie greifen).

    Defensiv: Unbekannte Werte in entry.data werden ignoriert, damit ein
    manuell editierter Config-Entry das Plugin nicht crasht.
    """
    raw = ""
    try:
        raw = entry.data.get(CONF_LANGUAGE, "") or ""
    except Exception:  # noqa: BLE001
        raw = ""
    if isinstance(raw, str) and raw in SUPPORTED_LANGUAGES:
        return raw
    return resolve_language(hass)
