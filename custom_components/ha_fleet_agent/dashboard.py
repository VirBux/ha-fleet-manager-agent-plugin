"""Auto-Dashboard 'Fernwartung' / 'Remote maintenance' fuer die HA Fleet Manager Agent Integration.

REQUIREMENTS Sec. 4.6 / TODO #91 — beim Einrichten des Plugins entsteht in der
HA-Instanz des Endkunden automatisch ein eigenes Lovelace-Dashboard
(url_path 'ha-fleet-manager', Storage-Mode) als Surface fuer die 9 Plugin-Entities.

**Sprache (TODO #100, Plugin 0.7.0/0.7.1):** Texte (Sidebar-Titel, Intro-Markdown,
Sektions-Headings, Tile-Namen) sind ueber ``_DASHBOARD_TEXTS`` zwei-sprachig
hinterlegt (DE/EN). Seit 0.7.1 waehlt der Endkunde die Sprache **explizit im
Config-Flow** — der Wert landet in ``entry.data[CONF_LANGUAGE]`` und wird
ueber ``_lang_from_entry`` ausgelesen. Frueher (0.7.0) wurde stattdessen
``hass.config.language`` herangezogen, was Probleme machte (HA-Profile-Sprache
pro User vs. System-Sprache; HAOS-Setup-Wizard setzt oft die falsche). Die
explizite Wahl ist kanonisch; ``hass.config.language`` greift nur noch als
Fallback fuer 0.7.0-Bestandsinstallationen. Nach Anlage wird die Sprache im
Flag-Store unter ``"language"`` persistiert; nach HA-Neustart wird das
Dashboard mit derselben Sprache wieder eingehaengt. Spaeterer HA-Sprachwechsel
laesst das Dashboard in der einmal gewaehlten Sprache (Lovelace-Karten-Configs
sind statisch in ``.storage/lovelace.<uuid>`` gespeichert — Storage-Mode-Limit
von HA). Reinstall wechselt die Sprache.

**Architektur:**

In aktuellem HA Core (>= 2024.x) hat ``LovelaceData`` KEIN
``dashboards_collection``-Attribut mehr — die interne ``DashboardsCollection``
ist nur eine lokale Variable im ``lovelace.async_setup`` und nirgendwo
persistent erreichbar. Eine eigene parallele ``DashboardsCollection``-Instanz
waere ein Storage-Konflikt: HA wuerde unsere Eintraege beim naechsten
eigenen ``async_save`` ueberschreiben (beide teilen denselben
``lovelace_dashboards``-Store).

Daher umgekehrter Weg:

- Wir erzeugen eine eigene ``LovelaceStorage`` mit einer eigenen, von uns
  vergebenen ``id`` (UUID, im Flag-Store gemerkt). Diese ``LovelaceStorage``
  haelt ihre Karten-Config in ``.storage/lovelace.<uuid>`` — voellig getrennt
  von HAs DashboardsCollection.
- Wir haengen sie direkt in ``hass.data[LOVELACE_DATA].dashboards[url_path]``
  und registrieren den Sidebar-Eintrag ueber ``frontend.async_register_built_in_panel``
  (genau wie HAs ``_register_panel``).
- Nach HA-Neustart: HAs DashboardsCollection enthaelt unser Dashboard NICHT
  (wir haben es nie dort eingetragen). Unser Plugin-Setup haengt es bei jedem
  Lauf wieder in ``LovelaceData.dashboards`` ein, gleicher ``id`` → derselbe
  Karten-Store → konsistente Sicht fuer den Endkunden.

**Idempotenz:** Eigener Flag-Store ``{DOMAIN}.dashboard`` mit
``{created, url_path, dashboard_id, template_version, language}``. Existiert
das Dashboard schon (vom Kunden manuell angelegt, gleicher ``url_path``),
wird es respektiert (kein Ueberschreiben). Das Plugin merkt sich beim ersten
Lauf seine eigene ``dashboard_id`` und ``language`` und hat damit dauerhaft
Zugriff auf seinen Karten-Store und den Sprachstand. Fehlt ``language`` im
Flag (Bestands-Installationen < 0.7.0), wird ``"de"`` angenommen (alter
Sidebar-Titel und Dashboard waren auf Deutsch).

**Cleanup:** Nur bei vollstaendiger Entfernung (``async_remove_entry``),
NICHT bei Reload. Loescht den ``lovelace.<uuid>``-Store, entfernt den
Sidebar-Eintrag und unseren Flag-Store.

Alle Lovelace-Zugriffe sind defensiv gekapselt — bricht das Setup nie.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_LANGUAGE,
    DEFAULT_LANGUAGE,
    DOMAIN,
    SUPPORTED_LANGUAGES,
)

_LOGGER = logging.getLogger(__name__)

DASHBOARD_URL_PATH = "ha-fleet-manager"
DASHBOARD_ICON = "mdi:remote-desktop"
DASHBOARD_TEMPLATE_VERSION = 1

_DASHBOARD_STORAGE_VERSION = 1
_DASHBOARD_STORAGE_KEY = f"{DOMAIN}.dashboard"

# Lovelace-Domain wird mehrfach als Panel-Component und HassKey gebraucht.
_LOVELACE_DOMAIN = "lovelace"

# Fuer Bestands-0.6.5-Flags (ohne ``language``-Feld): Dashboard war damals
# durchgaengig deutsch, also bleibt es das nach dem Update auch.
LEGACY_FLAG_LANGUAGE = "de"

# Mapping logischer Slot-Namen -> (platform, unique_id-suffix).
# Reihenfolge ist die Kartensequenz im Dashboard.
ENTITY_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("remote_access_status", "sensor", "_remote_access_status"),
    ("connection_state", "sensor", "_connection_state"),
    ("tunnel_active", "binary_sensor", "_tunnel_active"),
    ("preauth_expires_at", "sensor", "_preauth_expires_at"),
    ("session_ends_at", "sensor", "_session_ends_at"),
    ("pre_authorization", "switch", "_pre_authorization"),
    ("preauth_validity", "number", "_preauth_validity"),
    ("preauth_max_duration", "number", "_preauth_max_duration"),
    ("close_tunnel", "button", "_close_tunnel"),
)


# --------------------------------------------------------- Sprach-Texte
# Bewusst ein flaches Dict pro Sprache, damit Tests einzelne Strings
# referenzieren und der Builder ohne if/else-Verschachtelung auskommt.


_DASHBOARD_TEXTS: dict[str, dict[str, Any]] = {
    "de": {
        "dashboard_title": "Fernwartung",
        "sidebar_title": "Fernwartung",
        "header_subtitle": "**Fernwartung durch dein Smart-Home-Team.**",
        "intro_markdown": (
            "### Was ist dieses Dashboard?\n\n"
            "Dieses Dashboard hat das **HA Fleet Manager Agent**-Plugin automatisch für dich "
            "angelegt. Es ist die Software, über die dein Smart-Home-Team deine "
            "Home-Assistant-Installation sicher aus der Ferne warten kann — ohne "
            "dass du einen VPN-Zugang oder Port-Forwarding einrichten musst.\n\n"
            "### Wozu brauche ich es?\n\n"
            "Wenn dein Team etwas prüfen oder einstellen will, baut es einen "
            "verschlüsselten Tunnel zu deinem HA auf — **immer mit deiner "
            "Zustimmung**. Hier hast du jederzeit den Überblick und die Kontrolle:\n\n"
            "- du siehst, ob aktuell jemand verbunden ist,\n"
            "- du erteilst auf Wunsch eine **Vorab-Freigabe** für geplante Termine,\n"
            "- du beendest eine laufende Sitzung mit einem Klick.\n\n"
            "### Was muss ich tun?\n\n"
            "Im Alltag **nichts**. Will dein Team zugreifen, erscheint in Home "
            "Assistant unter *Einstellungen → Reparaturen* eine Anfrage mit Betreff, "
            "Anfragegrund und gewünschter Dauer — du wählst **Annehmen** oder "
            "**Ablehnen**.\n\n"
            "Dieses Dashboard ist nur für den Überblick und für **proaktive** "
            "Vorab-Freigaben (z.B. wenn ein Wartungstermin vereinbart ist und du "
            "nicht jedes Mal manuell zustimmen willst)."
        ),
        "status_section_title": "Status",
        "status_help": (
            "Auf einen Blick: was passiert gerade?\n\n"
            "- **Fernzugriffs-Status** — Gesamtzustand: *idle* (nichts läuft), "
            "*pre_authorized* (Vorab-Freigabe aktiv) oder *session_active* "
            "(Wartung läuft).\n"
            "- **Tunnel aktiv** — *Ja*, sobald dein Team gerade verbunden ist.\n"
            "- **Verbindungsstatus** — Verbindung des Plugins zum Fleet-Manager-"
            "Server. Sollte normalerweise *connected* sein.\n"
            "- **Vorab-Freigabe läuft ab** / **Aktive Sitzung endet** — Zeitpunkte, "
            "an denen eine Freigabe bzw. eine laufende Sitzung automatisch endet."
        ),
        "control_section_title": "Vorab-Freigabe (Steuerung)",
        "control_help": (
            "**Was ist eine Vorab-Freigabe?** Du erlaubst deinem Team, sich ohne "
            "weitere Nachfrage zu verbinden — innerhalb eines Zeitfensters, das du "
            "selbst bestimmst. Praktisch z.B. für einen vereinbarten "
            "Wartungstermin.\n\n"
            "1. **Vorab-Freigabe** einschalten,\n"
            "2. **Gültigkeitsdauer** wählen (wie lange darf die Freigabe genutzt "
            "werden, bevor sie verfällt — z.B. 8 h),\n"
            "3. **Max. Sitzungsdauer** wählen (wie lange darf eine einzelne "
            "Wartungssitzung maximal laufen — z.B. 4 h).\n\n"
            "Schalte die Vorab-Freigabe wieder aus, sobald du sie nicht mehr "
            "brauchst — du behältst so die volle Kontrolle."
        ),
        "action_section_title": "Aktionen",
        "action_help": (
            "Möchtest du eine laufende Wartungssitzung sofort beenden? Klick auf "
            "**Tunnel trennen** — die Verbindung deines Teams wird augenblicklich "
            "geschlossen.\n\n"
            "Eine bestehende **Vorab-Freigabe** bleibt davon unberührt: dein Team "
            "könnte sich theoretisch sofort wieder verbinden. Willst du das nicht, "
            "schalte zuerst oben die Vorab-Freigabe aus."
        ),
        "tiles": {
            "remote_access_status": "Fernzugriffs-Status",
            "connection_state": "Verbindungsstatus",
            "tunnel_active": "Tunnel aktiv",
            "preauth_expires_at": "Vorab-Freigabe läuft ab",
            "session_ends_at": "Aktive Sitzung endet",
            "pre_authorization": "Vorab-Freigabe",
            "preauth_validity": "Gültigkeitsdauer",
            "preauth_max_duration": "Max. Sitzungsdauer",
            "close_tunnel": "Tunnel trennen",
        },
    },
    "en": {
        "dashboard_title": "Remote maintenance",
        "sidebar_title": "Remote maintenance",
        "header_subtitle": "**Remote maintenance by your smart-home team.**",
        "intro_markdown": (
            "### What is this dashboard?\n\n"
            "This dashboard was created automatically by the **HA Fleet Manager Agent** "
            "plugin. It is the software your smart-home team uses to remotely "
            "service your Home Assistant installation — without you having to set "
            "up a VPN or port forwarding.\n\n"
            "### Why do I need it?\n\n"
            "When your team needs to check or change something, it builds an "
            "encrypted tunnel to your HA — **always with your consent**. Here you "
            "keep the overview and the control at all times:\n\n"
            "- you can see whether anyone is currently connected,\n"
            "- on request, you grant a **Pre-authorization** for scheduled "
            "appointments,\n"
            "- you end a running session with a single click.\n\n"
            "### What do I need to do?\n\n"
            "On a day-to-day basis, **nothing**. When your team wants to connect, "
            "a request appears in Home Assistant under *Settings → Repairs* with "
            "the subject, the reason and the requested duration — you choose "
            "**Accept** or **Reject**.\n\n"
            "This dashboard is only for the overview and for **proactive** "
            "pre-authorizations (for example when a maintenance appointment has "
            "been scheduled and you don't want to confirm manually every time)."
        ),
        "status_section_title": "Status",
        "status_help": (
            "At a glance: what is happening right now?\n\n"
            "- **Remote access status** — Overall state: *idle* (nothing running), "
            "*pre_authorized* (pre-authorization active) or *session_active* "
            "(maintenance in progress).\n"
            "- **Tunnel active** — *Yes* as soon as your team is currently "
            "connected.\n"
            "- **Connection status** — Connection from the plugin to the Fleet "
            "Manager server. Should normally be *connected*.\n"
            "- **Pre-authorization expires at** / **Active session ends** — Times "
            "at which a pre-authorization or a running session ends automatically."
        ),
        "control_section_title": "Pre-authorization (controls)",
        "control_help": (
            "**What is a pre-authorization?** You allow your team to connect "
            "without further confirmation — within a time window you choose "
            "yourself. Useful for example for a scheduled maintenance "
            "appointment.\n\n"
            "1. Switch on **Pre-authorization**,\n"
            "2. Pick the **Validity duration** (how long the pre-authorization "
            "may be used before it expires — e.g. 8 h),\n"
            "3. Pick the **Max. session duration** (how long a single maintenance "
            "session may run at most — e.g. 4 h).\n\n"
            "Switch the pre-authorization off again as soon as you don't need it "
            "any more — that way you stay in full control."
        ),
        "action_section_title": "Actions",
        "action_help": (
            "Want to end a running maintenance session immediately? Click "
            "**Close tunnel** — your team's connection is closed instantly.\n\n"
            "An existing **Pre-authorization** is left untouched: your team could "
            "in theory reconnect right away. If you don't want that, switch off "
            "the pre-authorization above first."
        ),
        "tiles": {
            "remote_access_status": "Remote access status",
            "connection_state": "Connection status",
            "tunnel_active": "Tunnel active",
            "preauth_expires_at": "Pre-authorization expires at",
            "session_ends_at": "Active session ends",
            "pre_authorization": "Pre-authorization",
            "preauth_validity": "Validity duration",
            "preauth_max_duration": "Max. session duration",
            "close_tunnel": "Close tunnel",
        },
    },
}


def _texts(lang: str) -> dict[str, Any]:
    """Liefert das Sprach-Dict; faellt auf ``DEFAULT_LANGUAGE`` zurueck."""
    return _DASHBOARD_TEXTS.get(lang, _DASHBOARD_TEXTS[DEFAULT_LANGUAGE])


def _resolve_language(hass: HomeAssistant) -> str:
    """Liest die HA-Sprache und mappt sie auf eine unterstuetzte Plugin-Sprache.

    ``hass.config.language`` kann ``"de"``, ``"de_DE"``, ``"en"``, ``"en_GB"``,
    ``"fr"`` usw. sein — wir schneiden auf den 2-Buchstaben-Praefix und mappen
    alles ausser ``"de"`` auf den ``DEFAULT_LANGUAGE`` (``"en"``).

    Wird seit Plugin 0.7.1 nur noch als **Fallback** verwendet: kanonische
    Quelle ist die im Config-Flow gewaehlte Sprache (``entry.data[CONF_LANGUAGE]``,
    siehe ``_lang_from_entry``). HA-Sprache greift nur, wenn das Feld im
    ConfigEntry fehlt (0.7.0-Bestand).
    """
    raw = ""
    try:
        raw = getattr(hass.config, "language", "") or ""
    except Exception:  # noqa: BLE001
        _LOGGER.debug("hass.config.language nicht lesbar — nutze Default")
    short = raw[:2].lower() if isinstance(raw, str) else ""
    return short if short in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def _lang_from_entry(entry: ConfigEntry, hass: HomeAssistant) -> str:
    """Liefert die Sprache fuer dieses ConfigEntry.

    Prioritaet:
    1. ``entry.data[CONF_LANGUAGE]`` — Endkunden-Wahl im Config-Flow
       (kanonische Quelle ab 0.7.1).
    2. ``_resolve_language(hass)`` — Fallback fuer 0.7.0-Bestandsinstallationen,
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
    return _resolve_language(hass)


# --------------------------------------------------------- Karten-Builder


def build_dashboard_config(
    entity_ids: dict[str, str | None], lang: str = DEFAULT_LANGUAGE
) -> dict[str, Any]:
    """Erzeugt die Lovelace-Storage-Config fuer das Fernwartungs-Dashboard.

    ``entity_ids`` ist ein Mapping wie es ``_resolve_entity_ids`` liefert
    (Slot-Name aus ``ENTITY_SLOTS`` -> entity_id oder ``None`` falls die
    Entity nicht registriert ist). Fehlt eine Entity, wird die Karte
    weggelassen; eine ansonsten leere Sektion entfaellt komplett.

    ``lang`` waehlt die Sprache der Texte (Sektions-Titel, Erklaer-Markdown,
    Tile-Namen). Unbekannte Sprachen fallen auf ``DEFAULT_LANGUAGE`` zurueck.

    Die Funktion ist rein deterministisch und ohne HA aufrufbar — der Test
    deckt sie als Plain-Dict-Builder ab.
    """

    t = _texts(lang)
    tiles_dict: dict[str, str] = t["tiles"]
    sections: list[dict[str, Any]] = []

    status_tiles = _tiles(
        entity_ids,
        [
            ("remote_access_status", tiles_dict["remote_access_status"], "mdi:access-point-network"),
            ("connection_state", tiles_dict["connection_state"], "mdi:cloud-check"),
            ("tunnel_active", tiles_dict["tunnel_active"], "mdi:tunnel"),
            ("preauth_expires_at", tiles_dict["preauth_expires_at"], "mdi:calendar-clock"),
            ("session_ends_at", tiles_dict["session_ends_at"], "mdi:timer-sand"),
        ],
    )
    if status_tiles:
        sections.append(
            _section(t["status_section_title"], t["status_help"], status_tiles)
        )

    control_tiles = _tiles(
        entity_ids,
        [
            ("pre_authorization", tiles_dict["pre_authorization"], "mdi:shield-key"),
            ("preauth_validity", tiles_dict["preauth_validity"], "mdi:clock-outline"),
            ("preauth_max_duration", tiles_dict["preauth_max_duration"], "mdi:timer-cog"),
        ],
    )
    if control_tiles:
        sections.append(
            _section(t["control_section_title"], t["control_help"], control_tiles)
        )

    action_tiles = _tiles(
        entity_ids,
        [("close_tunnel", tiles_dict["close_tunnel"], "mdi:close-network")],
    )
    if action_tiles:
        sections.append(
            _section(t["action_section_title"], t["action_help"], action_tiles)
        )

    # Breite Erklaer-Sektion immer voranstellen (auch wenn alle Entities fehlen,
    # erklaert sie wenigstens den Dashboard-Zweck und woher es kommt).
    head_section = {
        "type": "grid",
        "cards": [
            {
                "type": "markdown",
                "content": t["intro_markdown"],
                "grid_options": {"columns": "full"},
            }
        ],
        "column_span": 2,
        "background": {"opacity": 50},
    }
    sections.insert(0, head_section)

    return {
        "title": t["dashboard_title"],
        "views": [
            {
                "type": "sections",
                "title": t["dashboard_title"],
                "icon": DASHBOARD_ICON,
                "sections": sections,
                "max_columns": 2,
                "header": {
                    "card": {
                        "type": "markdown",
                        "text_only": True,
                        "content": t["header_subtitle"],
                    }
                },
            }
        ],
    }


def _section(
    title: str,
    help_markdown: str,
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Baut eine 'grid'-Sektion: Heading + Erklaer-Markdown + Karten.

    Die Erklaer-Karte steht VOR den Tiles, damit der Endkunde erst liest,
    wozu die Werte da sind, und dann auf das Bedien-/Anzeige-Element schaut.
    """
    return {
        "type": "grid",
        "cards": [
            {
                "type": "heading",
                "heading": title,
                "heading_style": "title",
            },
            {
                "type": "markdown",
                "content": help_markdown,
                "grid_options": {"columns": "full"},
            },
            *cards,
        ],
    }


def _tiles(
    entity_ids: dict[str, str | None],
    items: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Erzeugt Tile-Karten fuer alle Slots, deren entity_id aufgeloest werden konnte."""
    out: list[dict[str, Any]] = []
    for slot, name, icon in items:
        entity_id = entity_ids.get(slot)
        if not entity_id:
            continue
        out.append(
            {
                "type": "tile",
                "entity": entity_id,
                "name": name,
                "icon": icon,
            }
        )
    return out


# --------------------------------------------------------- entity-Aufloesung


def _resolve_entity_ids(hass: HomeAssistant, entry_id: str) -> dict[str, str | None]:
    """Loest alle Slot-Entities ueber die Entity-Registry auf (per unique_id).

    Fehlende Entries ergeben ``None`` — der Builder ueberspringt sie. Wir
    raten *nie* anhand der Friendly-Name-Slugs (die ergeben in deutschem
    HA ``switch.ha_fleet_agent_vorab_freigabe``, in englischem etwas anderes).
    """
    try:
        registry = er.async_get(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Entity-Registry nicht verfuegbar — Dashboard kann keine Entities aufloesen")
        return {slot: None for slot, _, _ in ENTITY_SLOTS}

    resolved: dict[str, str | None] = {}
    for slot, platform, suffix in ENTITY_SLOTS:
        unique_id = f"{entry_id}{suffix}"
        try:
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fehler beim Aufloesen von %s/%s", platform, unique_id)
            entity_id = None
        resolved[slot] = entity_id
    return resolved


# --------------------------------------------------------- Idempotenz-Store


def _dashboard_store(hass: HomeAssistant) -> Store:
    """Eigener Store fuer das 'angelegt'-Flag + die Dashboard-id (UUID) + Sprache.

    Bewusst NICHT mit ``preauth_config`` zusammengelegt — getrennter
    Lebenszyklus (Cleanup nur bei async_remove_entry).

    Inhalt: ``{created: bool, url_path: str, dashboard_id: str|None,
    template_version: int, language: str}``. ``dashboard_id`` ist ``None``,
    falls wir beim ersten Anlegen ein bereits existierendes Fremd-Dashboard
    mit demselben ``url_path`` respektiert haben (kein eigener Storage
    angelegt). ``language`` fehlt bei Bestands-Flags < 0.7.0 — Reattach
    nutzt dann ``LEGACY_FLAG_LANGUAGE`` (= ``"de"``).
    """
    return Store(hass, _DASHBOARD_STORAGE_VERSION, _DASHBOARD_STORAGE_KEY)


# --------------------------------------------------------- Lovelace-Calls


async def async_ensure_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Legt das Fernwartungs-Dashboard idempotent an.

    Ablauf:
    1. Store laden. Wenn ``created: True``:
       - Falls unser ``url_path`` schon in ``LovelaceData.dashboards`` — fertig.
       - Sonst (HA-Neustart, leere LovelaceData): unsere ``LovelaceStorage``
         mit der gemerkten ``dashboard_id`` wieder einhaengen + Panel
         registrieren. Karten sind im ``lovelace.<dashboard_id>``-Store
         bereits vorhanden → nicht ueberschreiben. Sprache aus Flag, Default
         ``LEGACY_FLAG_LANGUAGE`` falls Bestands-Flag.
    2. Sonst (frisch):
       - Sprache aus ``hass.config.language`` ableiten.
       - Falls unser ``url_path`` schon in ``LovelaceData.dashboards`` (Kunde
         oder andere Integration hat etwas angelegt) — respektieren, nur Flag
         setzen (``dashboard_id=None``, ``language`` aus HA).
       - Sonst: neue UUID generieren, ``LovelaceStorage`` anlegen, in
         ``LovelaceData.dashboards`` einhaengen, Panel registrieren,
         Karten-Config initial in der gewaehlten Sprache speichern, Flag
         setzen.
    """
    store = _dashboard_store(hass)
    try:
        flag = await store.async_load()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Konnte Dashboard-Flag nicht laden — ueberspringe Setup")
        return

    ld = _lovelace_data(hass)
    if ld is None:
        _LOGGER.debug(
            "LovelaceData noch nicht vorhanden — Dashboard wird beim naechsten "
            "Versuch angelegt (Start-Fallback im Caller)"
        )
        return

    dashboards = _lovelace_dashboards(ld)
    if dashboards is None:
        _LOGGER.warning("LovelaceData.dashboards nicht vorhanden — ueberspringe")
        return

    # 1. Bereits angelegt? Dann nur sicherstellen, dass es in LovelaceData haengt.
    if isinstance(flag, dict) and flag.get("created"):
        if DASHBOARD_URL_PATH in dashboards:
            _LOGGER.debug("Fernwartungs-Dashboard bereits in LovelaceData — nichts zu tun")
            return
        dashboard_id = flag.get("dashboard_id")
        if not dashboard_id:
            # Fremd-Dashboard wurde frueher respektiert — nichts neues anlegen.
            _LOGGER.debug(
                "Flag gesetzt ohne dashboard_id (Fremd-Dashboard respektiert) — nichts zu tun"
            )
            return
        # HA-Neustart: unsere LovelaceStorage wieder einhaengen — Sprache aus Flag.
        lang = flag.get("language") or LEGACY_FLAG_LANGUAGE
        if lang not in SUPPORTED_LANGUAGES:
            lang = DEFAULT_LANGUAGE
        _attach_storage_and_panel(
            hass, dashboards, dashboard_id, initial_config=None, lang=lang
        )
        return

    # 2. Frisch — Sprache aus dem ConfigEntry lesen (Endkunden-Wahl im Config-Flow).
    #    Fallback auf HA-Sprache fuer 0.7.0-Bestandsinstallationen.
    lang = _lang_from_entry(entry, hass)

    # 2a. Fremd-Dashboard mit unserem url_path? Respektieren.
    if DASHBOARD_URL_PATH in dashboards:
        _LOGGER.info(
            "Dashboard '%s' existiert bereits — ueberschreibe nicht, setze nur Flag",
            DASHBOARD_URL_PATH,
        )
        await _mark_created(store, dashboard_id=None, language=lang)
        return

    # 2b. Frisch anlegen.
    dashboard_id = uuid.uuid4().hex
    entity_ids = _resolve_entity_ids(hass, entry.entry_id)
    config = build_dashboard_config(entity_ids, lang)

    try:
        _attach_storage_and_panel(
            hass, dashboards, dashboard_id, initial_config=config, lang=lang
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Anlegen des Dashboards '%s' fehlgeschlagen", DASHBOARD_URL_PATH
        )
        return

    await _mark_created(store, dashboard_id=dashboard_id, language=lang)
    _LOGGER.info(
        "Fernwartungs-Dashboard '%s' angelegt (id=%s, lang=%s)",
        DASHBOARD_URL_PATH,
        dashboard_id,
        lang,
    )


async def async_remove_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Loescht das Dashboard und das Flag — nur bei vollstaendiger Entfernung.

    Reihenfolge:
    1. Sidebar-Panel entfernen (Endkunde sieht das Dashboard sofort weg).
    2. LovelaceStorage aus LovelaceData.dashboards loesen.
    3. ``lovelace.<dashboard_id>``-Store loeschen (Karten weg).
    4. Eigenes Flag-Store loeschen.
    """
    store = _dashboard_store(hass)
    try:
        flag = await store.async_load()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Konnte Dashboard-Flag nicht laden — Cleanup teilweise")
        flag = None

    # Frontend-Panel raus (defensiv — kein Crash, wenn Panel nicht da)
    _remove_panel(hass)

    # LovelaceData aufraeumen (LovelaceStorage entfernen + Store-Daten loeschen)
    ld = _lovelace_data(hass)
    storage_obj = None
    if ld is not None:
        dashboards = _lovelace_dashboards(ld)
        if dashboards is not None and DASHBOARD_URL_PATH in dashboards:
            storage_obj = dashboards.pop(DASHBOARD_URL_PATH, None)

    if storage_obj is not None:
        try:
            await storage_obj.async_delete()
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Konnte Karten-Storage fuer '%s' nicht loeschen", DASHBOARD_URL_PATH
            )

    await _clear_flag(store)
    if isinstance(flag, dict) and flag.get("dashboard_id"):
        _LOGGER.info(
            "Fernwartungs-Dashboard '%s' entfernt (id=%s)",
            DASHBOARD_URL_PATH,
            flag.get("dashboard_id"),
        )


# --------------------------------------------------------- Helpers


def _lovelace_data(hass: HomeAssistant):
    """Liefert ``hass.data[LOVELACE_DATA]`` defensiv (ohne harten Lovelace-Import)."""
    return hass.data.get(_LOVELACE_DOMAIN)


def _lovelace_dashboards(ld: Any) -> dict[str, Any] | None:
    """Greift defensiv auf ``LovelaceData.dashboards`` zu (Dataclass-Attribut)."""
    return getattr(ld, "dashboards", None)


def _attach_storage_and_panel(
    hass: HomeAssistant,
    dashboards: dict[str, Any],
    dashboard_id: str,
    *,
    initial_config: dict[str, Any] | None,
    lang: str,
) -> None:
    """Erzeugt LovelaceStorage, haengt sie in LovelaceData und registriert Panel.

    Wenn ``initial_config`` gesetzt ist, werden die Karten initial gespeichert
    (Erst-Anlage). Bei ``None`` wird der bestehende ``lovelace.<id>``-Store
    wiederverwendet (HA-Neustart-Fall). ``lang`` legt die Sprache des
    Sidebar-Titels und der Item-Metadaten fest.
    """
    # Lokale Imports, damit conftest-Stubs greifen koennen und das Plugin auch
    # in seltsamen Test-Umgebungen importierbar bleibt.
    from homeassistant.components import frontend
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    item = _build_dashboard_item(dashboard_id, lang)
    storage_obj = LovelaceStorage(hass, item)
    dashboards[DASHBOARD_URL_PATH] = storage_obj

    sidebar_title = _texts(lang)["sidebar_title"]
    frontend.async_register_built_in_panel(
        hass,
        _LOVELACE_DOMAIN,
        frontend_url_path=DASHBOARD_URL_PATH,
        require_admin=False,
        sidebar_title=sidebar_title,
        sidebar_icon=DASHBOARD_ICON,
        config={"mode": "storage"},
        update=False,
    )

    if initial_config is not None:
        # Karten initial speichern — landet in lovelace.<dashboard_id>-Store.
        # Schedule, damit der Caller nicht warten muss; Fehler wird geloggt.
        hass.async_create_task(_save_initial_config(storage_obj, initial_config))


async def _save_initial_config(storage_obj: Any, config: dict[str, Any]) -> None:
    try:
        await storage_obj.async_save(config)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Initiale Karten-Config konnte nicht gespeichert werden")


def _build_dashboard_item(dashboard_id: str, lang: str) -> dict[str, Any]:
    """Baut das Item-Dict, das ``LovelaceStorage`` als ``config`` erwartet.

    Felder analog zu HAs ``DashboardsCollection``-Eintraegen:
    ``id`` (CONFIG_STORAGE_KEY-Suffix), ``url_path``, ``title``, ``icon``,
    ``require_admin``, ``show_in_sidebar``, ``mode``.
    """
    return {
        "id": dashboard_id,
        "url_path": DASHBOARD_URL_PATH,
        "title": _texts(lang)["dashboard_title"],
        "icon": DASHBOARD_ICON,
        "require_admin": False,
        "show_in_sidebar": True,
        "mode": "storage",
    }


def _remove_panel(hass: HomeAssistant) -> None:
    """Entfernt das Frontend-Panel defensiv."""
    try:
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, DASHBOARD_URL_PATH)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Konnte Sidebar-Panel '%s' nicht entfernen", DASHBOARD_URL_PATH)


async def _mark_created(
    store: Store, *, dashboard_id: str | None, language: str
) -> None:
    try:
        await store.async_save(
            {
                "created": True,
                "url_path": DASHBOARD_URL_PATH,
                "dashboard_id": dashboard_id,
                "template_version": DASHBOARD_TEMPLATE_VERSION,
                "language": language,
            }
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Konnte Dashboard-Flag nicht speichern")


async def _clear_flag(store: Store) -> None:
    try:
        await store.async_remove()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Konnte Dashboard-Flag nicht entfernen")
