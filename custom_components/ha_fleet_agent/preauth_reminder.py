"""Erinnerung an eine dauerhafte Vorab-Freigabe (#167, REQUIREMENTS §4.3).

Eine Vorab-Freigabe ohne Ablaufdatum gilt bis zum Widerruf. Damit sie nicht in
Vergessenheit gerät, erinnert das Plugin alle 30 Tage per persistent notification
daran — das ersetzt die Befristung als Schutz. Wann erinnert wird, entscheidet der
``RemoteAccessManager`` (er kennt Freigabe und ``last_reminder_at``); dieses Modul
liefert nur Texte und Anzeige.

Sprache: wie beim Dashboard die im Config-Flow gewählte (``entry.data[CONF_LANGUAGE]``).
Persistent notifications kennen keine Übersetzungsschlüssel aus ``strings.json``.
Den Namen des Integrators kennt das Plugin nicht, die Erinnerung kommt deshalb ohne.
"""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import DEFAULT_LANGUAGE, DOMAIN

_TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "title": "Fernwartung: dauerhafte Vorab-Freigabe aktiv",
        "message": (
            "Für diese Home-Assistant-Installation gilt eine **Vorab-Freigabe ohne "
            "Ablaufdatum**. Dein Smart-Home-Team kann sich damit jederzeit ohne "
            "Rückfrage verbinden; jede einzelne Sitzung dauert höchstens {max_hours} h.\n\n"
            "Brauchst du die Freigabe nicht mehr, schalte im Dashboard **Fernwartung** "
            "die **Vorab-Freigabe** aus (oder unter *Einstellungen → Geräte & Dienste → "
            "HA Fleet Manager Agent*).\n\n"
            "Diese Erinnerung erscheint alle 30 Tage, solange die Freigabe besteht."
        ),
    },
    "en": {
        "title": "Remote maintenance: permanent pre-authorization active",
        "message": (
            "This Home Assistant installation has a **pre-authorization without an "
            "expiry date**. Your smart-home team can connect at any time without asking "
            "first; each individual session lasts at most {max_hours} h.\n\n"
            "If you no longer need it, switch off **Pre-authorization** in the "
            "**Remote maintenance** dashboard (or under *Settings → Devices & services → "
            "HA Fleet Manager Agent*).\n\n"
            "This reminder appears every 30 days as long as the pre-authorization exists."
        ),
    },
    "es": {
        "title": "Mantenimiento remoto: autorización previa permanente activa",
        "message": (
            "Esta instalación de Home Assistant tiene una **autorización previa sin "
            "fecha de caducidad**. Tu equipo de hogar inteligente puede conectarse en "
            "cualquier momento sin preguntar; cada sesión dura como máximo {max_hours} h.\n\n"
            "Si ya no la necesitas, desactiva la **Autorización previa** en el panel "
            "**Mantenimiento remoto** (o en *Ajustes → Dispositivos y servicios → "
            "HA Fleet Manager Agent*).\n\n"
            "Este recordatorio aparece cada 30 días mientras exista la autorización previa."
        ),
    },
    "fr": {
        "title": "Maintenance à distance : pré-autorisation permanente active",
        "message": (
            "Cette installation Home Assistant dispose d'une **pré-autorisation sans "
            "date d'expiration**. Votre équipe maison connectée peut s'y connecter à tout "
            "moment sans demander ; chaque session dure au maximum {max_hours} h.\n\n"
            "Si vous n'en avez plus besoin, désactivez la **Pré-autorisation** dans le "
            "tableau de bord **Maintenance à distance** (ou sous *Paramètres → Appareils "
            "et services → HA Fleet Manager Agent*).\n\n"
            "Ce rappel s'affiche tous les 30 jours tant que la pré-autorisation existe."
        ),
    },
    "hr": {
        "title": "Održavanje na daljinu: trajno predodobrenje je aktivno",
        "message": (
            "Za ovu Home Assistant instalaciju vrijedi **predodobrenje bez datuma "
            "isteka**. Tvoj tim za pametnu kuću može se u bilo kojem trenutku spojiti "
            "bez upita; svaka pojedinačna sesija traje najviše {max_hours} h.\n\n"
            "Ako ti više nije potrebno, isključi **Predodobrenje** na nadzornoj ploči "
            "**Održavanje na daljinu** (ili pod *Postavke → Uređaji i usluge → "
            "HA Fleet Manager Agent*).\n\n"
            "Ovaj podsjetnik pojavljuje se svakih 30 dana dok predodobrenje postoji."
        ),
    },
}


def notification_id(entry_id: str) -> str:
    """Eine Erinnerung je Installation — die nächste ersetzt die vorige."""
    return f"{DOMAIN}_preauth_reminder_{entry_id}"


def texts(lang: str) -> dict[str, str]:
    """Texte der Sprache; unbekannte Sprachen fallen auf ``DEFAULT_LANGUAGE`` zurück."""
    return _TEXTS.get(lang, _TEXTS[DEFAULT_LANGUAGE])


def async_show(hass: HomeAssistant, entry_id: str, lang: str, max_hours: int) -> None:
    """Zeigt die Erinnerung an die dauerhafte Vorab-Freigabe."""
    t = texts(lang)
    persistent_notification.async_create(
        hass,
        t["message"].format(max_hours=max_hours),
        title=t["title"],
        notification_id=notification_id(entry_id),
    )


def async_dismiss(hass: HomeAssistant, entry_id: str) -> None:
    """Entfernt eine noch sichtbare Erinnerung (Widerruf, Wechsel auf befristet)."""
    persistent_notification.async_dismiss(hass, notification_id(entry_id))
