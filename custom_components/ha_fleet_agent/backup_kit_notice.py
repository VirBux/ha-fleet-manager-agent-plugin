"""Hinweis in HA: Der Backup-Schlüssel ging als Notfallkit an Fleet Manager (#203, S6).

Fordert der Integrator ein Backup mit Notfallkit an, schickt das Plugin den HA-Backup-Schlüssel
mit. Die Rolle des Integrators genügt dafür (E5); als Ausgleich erfährt der Endkunde es in HA —
per persistent notification, sobald das Backend den Schlüssel angenommen hat. Eine Meldung je
Installation, die nächste ersetzt die vorige.

Sprache: wie beim Dashboard die im Config-Flow gewählte (``entry.data[CONF_LANGUAGE]``).
Persistent notifications kennen keine Übersetzungsschlüssel aus ``strings.json``.
"""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import DEFAULT_LANGUAGE, DOMAIN

_TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "title": "Backup-Schlüssel an Fleet Manager übermittelt",
        "message": (
            "Fleet Manager hat mit dem Backup vom {date} auch den **Backup-Schlüssel** dieser "
            "Installation erhalten. Er liegt dort neben dem Backup, bis es gelöscht wird "
            "(spätestens nach 24 Stunden), sofern ihn dein Smart-Home-Team nicht in Fleet "
            "Manager speichert.\n\n"
            "Mit diesem Schlüssel lassen sich alle Backups dieser Installation öffnen. "
            "Angefordert hat ihn dein Smart-Home-Team über Fleet Manager."
        ),
    },
    "en": {
        "title": "Backup key sent to Fleet Manager",
        "message": (
            "Along with the backup from {date}, Fleet Manager also received the **backup "
            "key** of this installation. It is stored there next to the backup until the "
            "backup is deleted (after 24 hours at the latest), unless your smart-home team "
            "saves it in Fleet Manager.\n\n"
            "This key opens all backups of this installation. Your smart-home team requested "
            "it through Fleet Manager."
        ),
    },
    "es": {
        "title": "Clave de copia de seguridad enviada a Fleet Manager",
        "message": (
            "Junto con la copia de seguridad del {date}, Fleet Manager también ha recibido la "
            "**clave de copia de seguridad** de esta instalación. Se guarda allí junto a la "
            "copia hasta que esta se elimine (como máximo tras 24 horas), salvo que tu equipo "
            "de hogar inteligente la guarde en Fleet Manager.\n\n"
            "Con esta clave se pueden abrir todas las copias de seguridad de esta instalación. "
            "La ha solicitado tu equipo de hogar inteligente a través de Fleet Manager."
        ),
    },
    "fr": {
        "title": "Clé de sauvegarde transmise à Fleet Manager",
        "message": (
            "Avec la sauvegarde du {date}, Fleet Manager a également reçu la **clé de "
            "sauvegarde** de cette installation. Elle y est conservée à côté de la sauvegarde "
            "jusqu'à sa suppression (au plus tard après 24 heures), sauf si votre équipe maison "
            "connectée l'enregistre dans Fleet Manager.\n\n"
            "Cette clé permet d'ouvrir toutes les sauvegardes de cette installation. Votre "
            "équipe maison connectée l'a demandée via Fleet Manager."
        ),
    },
    "hr": {
        "title": "Ključ sigurnosne kopije poslan u Fleet Manager",
        "message": (
            "Uz sigurnosnu kopiju od {date} Fleet Manager je primio i **ključ sigurnosne "
            "kopije** ove instalacije. Ondje se čuva uz sigurnosnu kopiju dok se ona ne izbriše "
            "(najkasnije nakon 24 sata), osim ako ga tvoj tim za pametnu kuću ne spremi u "
            "Fleet Manager.\n\n"
            "Tim se ključem mogu otvoriti sve sigurnosne kopije ove instalacije. Zatražio ga je "
            "tvoj tim za pametnu kuću putem Fleet Managera."
        ),
    },
}


def notification_id(entry_id: str) -> str:
    """Ein Hinweis je Installation — der nächste ersetzt den vorigen."""
    return f"{DOMAIN}_backup_emergency_kit_{entry_id}"


def texts(lang: str) -> dict[str, str]:
    """Texte der Sprache; unbekannte Sprachen fallen auf ``DEFAULT_LANGUAGE`` zurück."""
    return _TEXTS.get(lang, _TEXTS[DEFAULT_LANGUAGE])


def async_show(hass: HomeAssistant, entry_id: str, lang: str, date: str) -> None:
    """Zeigt den Hinweis, dass der Backup-Schlüssel an Fleet Manager ging."""
    t = texts(lang)
    persistent_notification.async_create(
        hass,
        t["message"].format(date=date),
        title=t["title"],
        notification_id=notification_id(entry_id),
    )
