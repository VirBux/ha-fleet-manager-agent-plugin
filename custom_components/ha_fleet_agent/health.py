"""Systemgesundheit je HA-Bestandteil — billige Innensicht (#147, Etappe 1).

Home Assistant ist kein einzelner Prozess. Der bestehende 60-Sekunden-Push
beweist nur, dass der Agent atmet; für den beobachteten Fall „die Oberfläche war
tot, während die Add-ons im Hintergrund weiterliefen" gab es bisher kein Signal.

Dieses Modul erhebt darum je Bestandteil einen Zustand — ``ok`` / ``warn`` /
``error`` — und legt ihn dem bestehenden Payload bei. Kein zusätzlicher
Transport, keine neue Verbindung nach außen, keine Rohdaten: nur ein paar
verdichtete Zahlen und je Bestandteil ein Kurzstatus mit Grund.

Lastbudget (bewusst klein gehalten, Leitplanke aus der Spec):

===========================  =========================  ======================
Prüfung                      Takt                       Kosten pro Durchlauf
===========================  =========================  ======================
Event-Loop-Lag               alle 10 s                  praktisch null
Unavailable-Quote            im 60-s-Tick des Reporters Mikrosekunden, kein I/O
Frontend: HTML + Bundle-HEAD alle 10 min                2 Requests über Loopback
Supervisor ``/resolution``   alle 10 min                ein lokaler Call
Recorder-Dateigröße          alle 10 min                ein ``os.stat``
===========================  =========================  ======================

In Summe deutlich weniger Arbeit als die 5-Sekunden-CPU-Messung, die ohnehin
jede Minute läuft.

Dazu kommt die **Auto-Eskalation**: schlägt eine der billigen Prüfungen an, fährt
der Agent von sich aus einen WebSocket-Selbsttest gegen den Kanal, über den die
Oberfläche lebt — gedrosselt auf höchstens einmal alle fünf Minuten. Solange alles
grün ist, kostet er exakt nichts, weil er nie stattfindet.

Fehlt ein Bestandteil auf dieser Installation (eine reine Container-Installation
hat weder Supervisor noch Add-ons), taucht er im Ergebnis **gar nicht** auf. Ein
dauerhaft grauer Punkt suggerierte dort ein Problem, wo keins ist.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import aiohttp

from .const import (
    HA_LOCAL_URL,
    HEALTH_COMPONENT_ADDONS,
    HEALTH_COMPONENT_CORE,
    HEALTH_COMPONENT_DATABASE,
    HEALTH_COMPONENT_FRONTEND,
    HEALTH_COMPONENT_INTEGRATIONS,
    HEALTH_COMPONENT_SUPERVISOR,
    HEALTH_COMPONENTS,
    HEALTH_DETAIL_MAX_LEN,
    HEALTH_FRONTEND_BUNDLE_PATTERNS,
    HEALTH_FRONTEND_SLOW_SECONDS,
    HEALTH_FRONTEND_TIMEOUT_SECONDS,
    HEALTH_LOOP_LAG_ERROR_MS,
    HEALTH_LOOP_LAG_INTERVAL_SECONDS,
    HEALTH_LOOP_LAG_WARN_MS,
    HEALTH_REASON_ADDON_ERROR,
    HEALTH_REASON_ADDON_STOPPED,
    HEALTH_REASON_ENTITIES_UNAVAILABLE,
    HEALTH_REASON_FRONTEND_BUNDLE_MISSING,
    HEALTH_REASON_FRONTEND_NO_BUNDLE_REF,
    HEALTH_REASON_FRONTEND_SLOW,
    HEALTH_REASON_FRONTEND_UNREACHABLE,
    HEALTH_REASON_INTEGRATION_SETUP_ERROR,
    HEALTH_REASON_INTEGRATION_SETUP_RETRY,
    HEALTH_REASON_LOOP_LAG,
    HEALTH_REASON_RECORDER_DB_LARGE,
    HEALTH_REASON_RECORDER_MISSING,
    HEALTH_REASON_SUPERVISOR_ISSUES,
    HEALTH_REASON_SUPERVISOR_UNHEALTHY,
    HEALTH_REASON_SUPERVISOR_UNREACHABLE,
    HEALTH_REASON_SUPERVISOR_UNSUPPORTED,
    HEALTH_REASON_WS_UNREACHABLE,
    HEALTH_RECORDER_DB_SUFFIXES,
    HEALTH_RECORDER_DB_WARN_BYTES,
    HEALTH_RECORDER_DEFAULT_DB_FILE,
    HEALTH_SLOW_CHECK_INTERVAL_SECONDS,
    HEALTH_STATUS_ERROR,
    HEALTH_STATUS_OK,
    HEALTH_STATUS_WARN,
    HEALTH_UNAVAILABLE_RATIO_WARN,
    HEALTH_WS_PROBE_CHECK_INTERVAL_SECONDS,
    HEALTH_WS_PROBE_EXPECTED_TYPE,
    HEALTH_WS_PROBE_MIN_INTERVAL_SECONDS,
    HEALTH_WS_PROBE_PATH,
    HEALTH_WS_PROBE_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Rang der Zustände: höher = schlimmer. Braucht es überall dort, wo mehrere
# Einzelbefunde zu EINEM Punkt zusammenfallen (mehrere Add-ons, mehrere
# Integrationen) — der schlechteste gewinnt, sonst überdeckt ein laufendes
# Add-on ein abgestürztes.
_RANK = {HEALTH_STATUS_OK: 0, HEALTH_STATUS_WARN: 1, HEALTH_STATUS_ERROR: 2}

# Entity-Zustände, die als „meldet gerade nichts Brauchbares" gelten.
_UNAVAILABLE_STATES = ("unavailable", "unknown")


def _worse(left: str, right: str) -> str:
    """Gibt den schlechteren der beiden Zustände zurück."""
    return left if _RANK[left] >= _RANK[right] else right


def _entry(status: str, reason: str | None = None, detail: str | None = None) -> dict[str, Any]:
    """Baut den Payload-Eintrag eines Bestandteils.

    ``reason`` ist ein stabiler Code, kein fertiger Satz — übersetzt wird in der
    Web-App (fünf Sprachen). ``detail`` nennt die konkret betroffenen Namen,
    damit der Tooltip „Z-Wave JS: setup_retry" sagen kann statt nur „gelb".
    """
    result: dict[str, Any] = {"status": status}
    if reason:
        result["reason"] = reason
    if detail:
        result["detail"] = detail[:HEALTH_DETAIL_MAX_LEN]
    return result


def _join(names: list[str]) -> str:
    """Namen zu einem kurzen ``detail``-String verbinden (alphabetisch, stabil)."""
    return ", ".join(sorted(names))


class HealthMonitor:
    """Erhebt laufend die Innensicht und liefert sie dem StateReporter zu.

    Lebenszyklus hängt am StateReporter: :meth:`start` beim Setup der
    Config-Entry, :meth:`stop` beim Entladen. Beide Hintergrund-Tasks sind
    ``asyncio``-Tasks ohne eigenen Thread.
    """

    def __init__(self, hass: Any, session: aiohttp.ClientSession) -> None:
        self._hass = hass
        self._session = session
        self._lag_task: asyncio.Task | None = None
        self._slow_task: asyncio.Task | None = None
        # Maximum des Loop-Lags seit dem letzten Push. Bewusst das Maximum und
        # nicht der Mittelwert: ein einzelner Vier-Sekunden-Hänger verschwindet
        # im Mittel, ist aber genau das, was ein Mensch als „UI tot" erlebt.
        self._loop_lag_max_ms = 0.0
        # Ergebnisse der 10-Minuten-Prüfungen, zwischen den Läufen gehalten.
        # None = noch nie gelaufen bzw. auf dieser Installation nicht anwendbar;
        # solche Bestandteile fallen aus dem Payload heraus.
        self._frontend: dict[str, Any] | None = None
        self._database: dict[str, Any] | None = None
        self._supervisor: dict[str, Any] | None = None
        # Auto-Eskalation: letzter Selbsttest und wann er lief.
        self._escalation_task: asyncio.Task | None = None
        self._ws_probe: dict[str, Any] | None = None
        self._ws_probe_at: float | None = None

    # --------------------------------------------------------- Lebenszyklus

    def start(self) -> None:
        """Startet Lag-Messung und den 10-Minuten-Takt der übrigen Prüfungen."""
        loop = asyncio.get_event_loop()
        self._lag_task = loop.create_task(self._loop_lag_worker())
        self._slow_task = loop.create_task(self._slow_check_worker())
        self._escalation_task = loop.create_task(self._escalation_worker())

    def stop(self) -> None:
        """Bricht alle Hintergrund-Tasks ab."""
        for task in (self._lag_task, self._slow_task, self._escalation_task):
            if task is not None and not task.done():
                task.cancel()
        self._lag_task = None
        self._slow_task = None
        self._escalation_task = None

    # --------------------------------------------------------- Event-Loop-Lag

    async def _loop_lag_worker(self) -> None:
        """Misst die Abweichung zwischen geplanter und echter Schlafdauer.

        Der Task plant einen ``asyncio.sleep`` über ein festes Fenster. Hängt
        der Event-Loop, kommt er zu spät wieder dran — die Differenz IST der
        Lag. Das ist derselbe Kanal, über den auch die Oberfläche lebt, und
        kostet pro Messung praktisch nichts.
        """
        loop = asyncio.get_event_loop()
        while True:
            started = loop.time()
            try:
                await asyncio.sleep(HEALTH_LOOP_LAG_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            lag_ms = (loop.time() - started - HEALTH_LOOP_LAG_INTERVAL_SECONDS) * 1000.0
            if lag_ms > self._loop_lag_max_ms:
                self._loop_lag_max_ms = lag_ms

    # --------------------------------------------------------- 10-Minuten-Takt

    async def _slow_check_worker(self) -> None:
        """Führt die drei teureren Prüfungen aus — sofort und dann alle 10 min.

        Sofort beim Start, weil der erste State-Push nicht 10 Minuten auf einen
        Frontend-Befund warten soll; die Karte wäre bis dahin ohne Aussage.
        """
        while True:
            try:
                await self._run_slow_checks()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — eine Prüfung darf den Takt nie killen
                _LOGGER.debug("Gesundheitsprüfungen fehlgeschlagen", exc_info=True)
            try:
                await asyncio.sleep(HEALTH_SLOW_CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    async def _run_slow_checks(self) -> None:
        """Führt die drei Prüfungen aus — jede für sich gekapselt.

        Einzeln und nicht als Block: fällt eine Prüfung mit einem Fehler aus, den ihre
        eigenen ``except``-Klauseln nicht kennen, sollen die beiden anderen trotzdem
        laufen. Sonst behielten sie ihren alten Wert, und zwar möglicherweise für
        immer, weil der nächste Durchlauf an derselben Stelle scheitert.
        """
        self._frontend = await self._safe_check(self._check_frontend, "frontend", self._frontend)
        self._database = await self._safe_check(self._check_database, "database", self._database)
        self._supervisor = await self._safe_check(
            self._check_supervisor, "supervisor", self._supervisor
        )
        _LOGGER.debug(
            "Gesundheitsprüfungen gelaufen — frontend=%s database=%s supervisor=%s",
            self._frontend,
            self._database,
            self._supervisor,
        )

    @staticmethod
    async def _safe_check(check: Any, name: str, previous: dict[str, Any] | None) -> dict[str, Any] | None:
        """Führt eine einzelne Prüfung aus; bei einem unerwarteten Fehler bleibt der
        letzte bekannte Befund stehen.

        Den alten Wert zu behalten ist die ehrlichere Wahl: dass unsere Prüfung
        gestolpert ist, sagt nichts über den Zustand des geprüften Bestandteils — ihn
        deshalb aus der Karte zu nehmen oder auf grün zu setzen, wäre beides gelogen.
        """
        try:
            return await check()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Gesundheitsprüfung '%s' fehlgeschlagen", name, exc_info=True)
            return previous

    # --------------------------------------------------------- Auto-Eskalation

    async def _escalation_worker(self) -> None:
        """Fährt den WebSocket-Selbsttest — aber nur bei Verdacht.

        Der Wächter selbst kostet nichts: er vergleicht alle 30 Sekunden zwei
        Zahlen. Erst wenn eine der billigen Prüfungen anschlägt, entsteht
        überhaupt Netzwerkverkehr, und auch dann höchstens einmal alle fünf
        Minuten — sonst hämmert eine dauerhaft kranke Instanz sich selbst, und
        zwar genau dann, wenn es ihr ohnehin schlecht geht.
        """
        loop = asyncio.get_event_loop()
        while True:
            try:
                await asyncio.sleep(HEALTH_WS_PROBE_CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            if not self._suspicious():
                continue
            now = loop.time()
            if (
                self._ws_probe_at is not None
                and now - self._ws_probe_at < HEALTH_WS_PROBE_MIN_INTERVAL_SECONDS
            ):
                continue
            self._ws_probe_at = now
            try:
                self._ws_probe = await self._ws_self_test()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — ein Selbsttest darf nie den Task killen
                _LOGGER.debug("WebSocket-Selbsttest fehlgeschlagen", exc_info=True)
                self._ws_probe = None
            else:
                _LOGGER.debug("WebSocket-Selbsttest: %s", self._ws_probe)

    def _suspicious(self) -> bool:
        """Gibt es einen Grund, den teuren Test zu fahren?

        Genau zwei Auslöser, beide aus der Spec: der Event-Loop hängt spürbar,
        oder die Oberfläche liefert ihr Bundle nicht mehr aus. Beides sind
        Hinweise darauf, dass Home Assistant zwar läuft, aber nicht mehr
        bedienbar ist — und genau das soll der Selbsttest bestätigen oder
        entkräften.

        Der Lag wird hier nur *gelesen*; zurückgesetzt wird er ausschließlich
        beim Push, sonst verlöre der Kern-Punkt seine Messgrundlage.
        """
        if self._loop_lag_max_ms >= HEALTH_LOOP_LAG_WARN_MS:
            return True
        if bool(self._frontend) and self._frontend.get("status") == HEALTH_STATUS_ERROR:
            return True
        # Ein bereits fehlgeschlagener Selbsttest haelt sich selbst am Leben. Ohne das
        # bliebe sein Befund bis zum Ende des Frischefensters stehen, auch wenn sich die
        # Instanz längst erholt hat: der Lag ist dann wieder normal, es gäbe also
        # keinen Verdacht mehr und damit auch keinen neuen Test, der den alten Befund
        # widerlegen könnte — der Kern-Punkt bliebe bis zu zehn Minuten grundlos rot.
        return bool(self._ws_probe) and self._ws_probe.get("result") == "failed"

    async def _ws_self_test(self) -> dict[str, Any]:
        """Öffnet die WebSocket-API und wartet auf HAs ``auth_required``.

        Das ist der Kanal, über den die Oberfläche lebt — und anders als die
        statischen Dateien, die aiohttp aus dem Cache bedient, entsteht das
        ``auth_required``-Frame im WebSocket-Handler und damit **im
        Event-Loop**. Hängt der, bleibt das Frame aus, obwohl der Port noch
        Verbindungen annimmt. Genau diese Lücke schließt der Test.

        **Es wird bewusst nicht authentifiziert.** Ein ``auth``-Frame mit
        ungültigem Token liefe in HAs ``process_wrong_login``, und nach genügend
        Fehlversuchen sperrt der IP-Ban-Mechanismus 127.0.0.1 aus — dieselbe
        Adresse, über die auch der Wartungstunnel läuft. Ein Diagnose-Werkzeug,
        das im Fehlerfall die Fernwartung aussperrt, wäre die schlechteste
        denkbare Eigenschaft. Einen gültigen Token gibt es nicht: der
        Integrator-User ist fail-closed (#110).

        Rückgabe: ``{"result": "ok"|"failed", "ms": int, "reason": str}``.
        """
        url = HA_LOCAL_URL.replace("http://", "ws://", 1) + HEALTH_WS_PROBE_PATH
        timeout = aiohttp.ClientTimeout(total=HEALTH_WS_PROBE_TIMEOUT_SECONDS)
        started = time.monotonic()
        try:
            async with self._session.ws_connect(url, timeout=timeout) as ws:
                message = await asyncio.wait_for(
                    ws.receive_json(), timeout=HEALTH_WS_PROBE_TIMEOUT_SECONDS
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)
                # Sauber schließen, statt die Verbindung offen zu lassen: eine
                # halb offene Session je Verdachtsfall summiert sich auf einer
                # kranken Instanz.
                await ws.close()
                if message.get("type") == HEALTH_WS_PROBE_EXPECTED_TYPE:
                    return {"result": "ok", "ms": elapsed_ms}
                return {
                    "result": "failed",
                    "ms": elapsed_ms,
                    "reason": f"unexpected:{message.get('type')}",
                }
        except (asyncio.TimeoutError, asyncio.CancelledError) as err:
            if isinstance(err, asyncio.CancelledError):
                raise
            return {
                "result": "failed",
                "ms": int((time.monotonic() - started) * 1000),
                "reason": "timeout",
            }
        except (aiohttp.ClientError, ValueError, TypeError) as err:
            return {
                "result": "failed",
                "ms": int((time.monotonic() - started) * 1000),
                "reason": type(err).__name__,
            }

    def _ws_probe_fresh(self) -> dict[str, Any] | None:
        """Der letzte Selbsttest, sofern er noch etwas über das Jetzt aussagt.

        Ein Befund von vor einer Stunde darf die Ampel nicht mehr beeinflussen —
        weder in die eine noch in die andere Richtung. Das Fenster ist das
        Doppelte des Drosselintervalls: großzügig genug, dass ein Test zwischen
        zwei Pushes nicht verfällt.
        """
        if self._ws_probe is None or self._ws_probe_at is None:
            return None
        age = asyncio.get_event_loop().time() - self._ws_probe_at
        if age > HEALTH_WS_PROBE_MIN_INTERVAL_SECONDS * 2:
            return None
        return self._ws_probe

    # --------------------------------------------------------- Oberfläche

    async def _check_frontend(self) -> dict[str, Any] | None:
        """HTML der Startseite holen, Bundle-Pfad lesen, auf diesen ein HEAD.

        Ausdrücklich **kein** Statuscode-Check auf die Startseite: eine tote
        Instanz liefert das HTML-Dokument weiterhin mit 200, während
        ``core.*.js``, die Fonts und die Icons fehlschlagen — der Nutzer sieht
        ein Alt-Text-Gerippe, ein Statuscode-Check meldete „gesund".

        Der Bundle-Pfad wird aus dem HTML **gelesen**, nicht konstruiert: HA
        2026.6 kennt nur noch ``/frontend_latest/``, ältere Versionen hatten
        zusätzlich ``/frontend_es5/``, und der Hash wechselt mit jeder Version.

        Beides läuft über Loopback (``127.0.0.1``), also kein Tunnel-Traffic und
        kein Reverse-Proxy, der das Ergebnis verfälschen könnte.
        """
        timeout = aiohttp.ClientTimeout(total=HEALTH_FRONTEND_TIMEOUT_SECONDS)
        started = time.monotonic()
        try:
            async with self._session.get(HA_LOCAL_URL + "/", timeout=timeout) as resp:
                if resp.status >= 400:
                    return _entry(
                        HEALTH_STATUS_ERROR,
                        HEALTH_REASON_FRONTEND_UNREACHABLE,
                        f"HTTP {resp.status}",
                    )
                html = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError, UnicodeDecodeError) as err:
            return _entry(
                HEALTH_STATUS_ERROR,
                HEALTH_REASON_FRONTEND_UNREACHABLE,
                type(err).__name__,
            )

        match = None
        for pattern in HEALTH_FRONTEND_BUNDLE_PATTERNS:
            match = re.search(pattern, html)
            if match is not None:
                break
        if match is None:
            # Kein Bundle referenziert: entweder hat HA sein Ausliefer-Schema
            # geändert oder das HTML ist nicht das erwartete. Beides ist kein
            # bewiesener Defekt — gelb, nicht rot.
            return _entry(HEALTH_STATUS_WARN, HEALTH_REASON_FRONTEND_NO_BUNDLE_REF)
        bundle_path = match.group(1)

        try:
            async with self._session.head(
                HA_LOCAL_URL + bundle_path, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    return _entry(
                        HEALTH_STATUS_ERROR,
                        HEALTH_REASON_FRONTEND_BUNDLE_MISSING,
                        f"HTTP {resp.status} {bundle_path}",
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            return _entry(
                HEALTH_STATUS_ERROR,
                HEALTH_REASON_FRONTEND_BUNDLE_MISSING,
                type(err).__name__,
            )

        elapsed = time.monotonic() - started
        if elapsed > HEALTH_FRONTEND_SLOW_SECONDS:
            return _entry(
                HEALTH_STATUS_WARN,
                HEALTH_REASON_FRONTEND_SLOW,
                f"{int(elapsed * 1000)} ms",
            )
        return _entry(HEALTH_STATUS_OK)

    # --------------------------------------------------------- Datenbank

    async def _check_database(self) -> dict[str, Any] | None:
        """Recorder: läuft er, und ist die SQLite-Datei ungewöhnlich groß?

        Der DB-Pfad kommt aus ``get_instance(hass).db_url`` — dem öffentlichen
        Recorder-Helfer, den auch andere Integrationen nutzen. Nur er
        unterscheidet SQLite von einer externen MariaDB/PostgreSQL: bei einer
        externen DB entfällt die Größenprüfung, und der Punkt bleibt trotzdem
        grün, statt fälschlich „keine Aussage" zu melden.

        Ist der Recorder gar nicht geladen, ist das kein fehlender Bestandteil,
        sondern ein Befund: ohne ihn gibt es keine Historie. Wer ihn bewusst
        abgeschaltet hat, sieht hier dauerhaft Rot — bewusst, denn genau diese
        Konfiguration ist für ein überwachtes System ungewöhnlich.
        """
        db_url = await self._resolve_recorder_db_url()
        if db_url is None:
            return _entry(HEALTH_STATUS_ERROR, HEALTH_REASON_RECORDER_MISSING)

        db_path = self._sqlite_path(db_url)
        if db_path is None:
            # Externe DB — läuft, aber die Dateigröße ist von hier aus nicht
            # messbar. Das ist keine Lücke, sondern eine andere Bauweise.
            return _entry(HEALTH_STATUS_OK)

        total = await self._hass.async_add_executor_job(self._db_size_bytes, db_path)
        if total is None:
            # Recorder meldet SQLite, die Datei ist aber (noch) nicht da: direkt
            # nach dem Start normal, sonst harmlos. Kein Defekt.
            return _entry(HEALTH_STATUS_OK)
        if total > HEALTH_RECORDER_DB_WARN_BYTES:
            return _entry(
                HEALTH_STATUS_WARN,
                HEALTH_REASON_RECORDER_DB_LARGE,
                f"{total / (1024 ** 3):.1f} GB",
            )
        return _entry(HEALTH_STATUS_OK)

    async def _resolve_recorder_db_url(self) -> str | None:
        """``db_url`` des laufenden Recorders, oder ``None``, wenn er fehlt."""
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Recorder-Helfer nicht importierbar", exc_info=True)
            return None
        try:
            instance = get_instance(self._hass)
        except Exception:  # noqa: BLE001 — wirft, wenn der Recorder nicht läuft
            _LOGGER.debug("get_instance() ohne laufenden Recorder", exc_info=True)
            return None
        url = getattr(instance, "db_url", None)
        if url:
            return str(url)
        # Recorder läuft, verrät die URL aber nicht (API-Änderung): dann der
        # Standardpfad im Config-Verzeichnis als Rückfallebene.
        return "sqlite:///" + self._hass.config.path(HEALTH_RECORDER_DEFAULT_DB_FILE)

    @staticmethod
    def _sqlite_path(db_url: str) -> str | None:
        """Dateipfad aus einer SQLite-``db_url``; ``None`` bei externer DB.

        ``sqlite:////config/home-assistant_v2.db`` (vier Slashes, absoluter
        Pfad) und ``sqlite:///relativ.db`` kommen beide vor.
        """
        if not db_url.startswith("sqlite://"):
            return None
        path = db_url[len("sqlite://") :]
        if path.startswith("/"):
            path = path[1:]
            if not path.startswith("/"):
                path = "/" + path
        return path or None

    @staticmethod
    def _db_size_bytes(db_path: str) -> int | None:
        """Größe der DB inklusive WAL-Datei; ``None``, wenn die DB fehlt.

        Bei aktivem WAL-Modus — HAs Standard — liegt ein erheblicher Teil der
        frischen Daten in ``-wal``; die Hauptdatei allein unterschätzt.
        """
        total = 0
        found = False
        for suffix in HEALTH_RECORDER_DB_SUFFIXES:
            try:
                total += os.stat(db_path + suffix).st_size
                if suffix == "":
                    found = True
            except OSError:
                continue
        return total if found else None

    # --------------------------------------------------------- Supervisor

    async def _check_supervisor(self) -> dict[str, Any] | None:
        """Supervisor-Zustand aus ``/resolution/info``.

        Auf der Test-VM verifiziert: ``/resolution/info`` liefert **keine**
        Felder ``healthy``/``supported`` (die stehen in ``/supervisor/info``),
        sondern die Listen ``unhealthy``, ``unsupported``, ``issues`` und
        ``suggestions``. Der Zustand wird darum daraus abgeleitet — eine
        nicht-leere ``unhealthy``-Liste IST „unhealthy".

        ``None`` (Bestandteil fehlt) bei jeder Installation ohne Supervisor:
        kein ``SUPERVISOR_TOKEN``, also HA Core im Container oder als venv.
        """
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return None

        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with self._session.get(
                "http://supervisor/resolution/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    return _entry(
                        HEALTH_STATUS_ERROR,
                        HEALTH_REASON_SUPERVISOR_UNREACHABLE,
                        f"HTTP {resp.status}",
                    )
                body = await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as err:
            return _entry(
                HEALTH_STATUS_ERROR,
                HEALTH_REASON_SUPERVISOR_UNREACHABLE,
                type(err).__name__,
            )

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return _entry(HEALTH_STATUS_WARN, HEALTH_REASON_SUPERVISOR_UNREACHABLE)

        unhealthy = [str(x) for x in (data.get("unhealthy") or [])]
        if unhealthy:
            return _entry(
                HEALTH_STATUS_ERROR,
                HEALTH_REASON_SUPERVISOR_UNHEALTHY,
                _join(unhealthy),
            )
        unsupported = [str(x) for x in (data.get("unsupported") or [])]
        if unsupported:
            return _entry(
                HEALTH_STATUS_WARN,
                HEALTH_REASON_SUPERVISOR_UNSUPPORTED,
                _join(unsupported),
            )
        issues = data.get("issues") or []
        if issues:
            # Ein Issue trägt `type` (was) und `reference` (woran) — beides ist
            # für einen Tooltip brauchbar, das rohe UUID-Feld nicht.
            names = [
                str(i.get("reference") or i.get("type"))
                for i in issues
                if isinstance(i, dict)
            ]
            return _entry(
                HEALTH_STATUS_WARN,
                HEALTH_REASON_SUPERVISOR_ISSUES,
                _join([n for n in names if n and n != "None"]),
            )
        return _entry(HEALTH_STATUS_OK)

    # --------------------------------------------------------- Add-ons

    @staticmethod
    def _check_addons(addons: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        """Add-on-Zustand aus der Liste, die der Reporter ohnehin schon hat.

        Quelle ist ``payload["addons"]`` (#102) mit dem bereits normalisierten
        ``status``: ``running`` / ``stopped`` / ``error``. Kein eigener Call.

        ``error`` ist rot — der Supervisor setzt ihn unter anderem, wenn ein
        Add-on wiederholt abstürzt und neu gestartet wird. ``stopped`` ist gelb:
        oft gewollt, manchmal aber der eigentliche Ausfall, und die Trennung
        Gelb/Rot ist genau dafür da.

        ``None`` (Bestandteil fehlt) bei leerer Liste: eine Installation ohne
        Supervisor kennt keine Add-ons, und ein grauer Punkt suggerierte dort
        ein Problem, wo keins ist.
        """
        if not addons:
            return None
        failed = [a.get("name") or a.get("slug") for a in addons if a.get("status") == "error"]
        if failed:
            return _entry(
                HEALTH_STATUS_ERROR,
                HEALTH_REASON_ADDON_ERROR,
                _join([str(n) for n in failed if n]),
            )
        stopped = [
            a.get("name") or a.get("slug") for a in addons if a.get("status") == "stopped"
        ]
        if stopped:
            return _entry(
                HEALTH_STATUS_WARN,
                HEALTH_REASON_ADDON_STOPPED,
                _join([str(n) for n in stopped if n]),
            )
        return _entry(HEALTH_STATUS_OK)

    # --------------------------------------------------------- Integrationen

    def _check_integrations(self, unavailable_ratio: float | None) -> dict[str, Any]:
        """Config-Entries nach ``setup_retry`` (gelb) und ``setup_error`` (rot).

        Die vorhandene ``integrations``-Liste des Reporters taugt hier nicht:
        sie wirft beide Zustände in einen Topf (``"error"``). Für die Ampel ist
        der Unterschied aber der Kern — ``setup_retry`` heißt „versucht es noch"
        und braucht bestenfalls einen Blick, ``setup_error`` heißt „aufgegeben"
        und braucht jemanden.

        Die Unavailable-Quote fällt hier mit ein: springt sie von wenigen
        Prozent auf ein Drittel, ist meist ein Funk-Stick oder ein Add-on weg,
        lange bevor jemand anruft. Ein eigener Punkt wäre es wert, überlädt aber
        die Karte (Spec).
        """
        from .const import DOMAIN  # noqa: PLC0415 — Zirkularimport vermeiden

        status = HEALTH_STATUS_OK
        reason: str | None = None
        detail: str | None = None
        try:
            from homeassistant.config_entries import ConfigEntryState  # noqa: PLC0415

            retry: list[str] = []
            error: list[str] = []
            for entry in self._hass.config_entries.async_entries():
                if entry.domain == DOMAIN:
                    continue
                if getattr(entry, "disabled_by", None) is not None:
                    continue
                if entry.state == ConfigEntryState.SETUP_RETRY:
                    retry.append(entry.domain)
                elif entry.state in (
                    ConfigEntryState.SETUP_ERROR,
                    ConfigEntryState.MIGRATION_ERROR,
                ):
                    error.append(entry.domain)
            if error:
                status = HEALTH_STATUS_ERROR
                reason = HEALTH_REASON_INTEGRATION_SETUP_ERROR
                detail = _join(set(error))
            elif retry:
                status = HEALTH_STATUS_WARN
                reason = HEALTH_REASON_INTEGRATION_SETUP_RETRY
                detail = _join(set(retry))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Integrations-Zustände nicht lesbar", exc_info=True)

        # Die Quote hebt höchstens auf Gelb an — ein bestätigter setup_error
        # bleibt der wichtigere Befund und darf nicht überschrieben werden.
        if (
            unavailable_ratio is not None
            and unavailable_ratio >= HEALTH_UNAVAILABLE_RATIO_WARN
            and status == HEALTH_STATUS_OK
        ):
            status = HEALTH_STATUS_WARN
            reason = HEALTH_REASON_ENTITIES_UNAVAILABLE
            detail = f"{unavailable_ratio:.0f} %"
        return _entry(status, reason, detail)

    # --------------------------------------------------------- Kern

    def _check_core(self, loop_lag_ms: float) -> dict[str, Any]:
        """Kern-Zustand aus dem Loop-Lag, geschärft durch den Selbsttest.

        „Loop steht komplett / kein Heartbeat" kann diese Prüfung prinzipbedingt
        nicht melden — dann käme auch der Push nicht mehr an. Das erkennt das
        Backend an der ausbleibenden Meldung; hier geht es um den Zustand
        dazwischen: HA lebt, reagiert aber träge.

        Liegt ein frischer Selbsttest vor, entscheidet er mit:

        - **Fehlgeschlagen** → rot mit ``ws_unreachable``, unabhängig vom Lag.
          Der Kanal, über den die Oberfläche lebt, antwortet nicht; das ist der
          härtere Befund und der brauchbarere Grund.
        - **Erfolgreich, aber Lag über der Fehlerschwelle** → nur gelb. Home
          Assistant antwortet nachweislich über genau den Kanal, der zählt; ein
          hoher Lag-Ausschlag allein rechtfertigt dann kein Rot. Genau dafür
          gibt es die Eskalation: sie kann einen Verdacht auch **entkräften**.
        """
        probe = self._ws_probe_fresh()
        if probe is not None and probe.get("result") == "failed":
            return _entry(
                HEALTH_STATUS_ERROR,
                HEALTH_REASON_WS_UNREACHABLE,
                str(probe.get("reason") or ""),
            )

        if loop_lag_ms >= HEALTH_LOOP_LAG_ERROR_MS:
            confirmed = probe is None or probe.get("result") != "ok"
            status = HEALTH_STATUS_ERROR if confirmed else HEALTH_STATUS_WARN
        elif loop_lag_ms >= HEALTH_LOOP_LAG_WARN_MS:
            status = HEALTH_STATUS_WARN
        else:
            return _entry(HEALTH_STATUS_OK)
        return _entry(status, HEALTH_REASON_LOOP_LAG, f"{int(loop_lag_ms)} ms")

    # --------------------------------------------------------- Unavailable

    def _unavailable_ratio(self) -> float | None:
        """Anteil ``unavailable``/``unknown`` an allen Entities, in Prozent.

        Reine Zählung über ``hass.states`` — Mikrosekunden, kein I/O.
        """
        states = self._hass.states.async_all()
        if not states:
            return None
        bad = sum(1 for s in states if getattr(s, "state", None) in _UNAVAILABLE_STATES)
        return round(bad / len(states) * 100.0, 1)

    # --------------------------------------------------------- Ergebnis

    def collect(self, addons: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Baut den ``health``-Block für den State-Payload.

        Wird im 60-Sekunden-Tick des Reporters aufgerufen. Der Lag-Höchstwert
        wird dabei zurückgesetzt: die gemeldete Zahl bezieht sich immer auf das
        Fenster seit dem letzten Push, nicht auf die gesamte Laufzeit.

        ``addons`` reicht der Reporter durch, statt die Supervisor-Info ein
        zweites Mal zu holen.
        """
        loop_lag_ms = self._loop_lag_max_ms
        self._loop_lag_max_ms = 0.0
        unavailable_ratio = self._unavailable_ratio()

        # Erst alle Befunde sammeln, dann in EINER definierten Reihenfolge einfügen.
        # Die kommt aus HEALTH_COMPONENTS und nicht aus dieser Funktion: die
        # Anzeige-Reihenfolge ist genau einmal festgelegt, und die Liste bleibt der
        # Ort, an dem ein siebter Bestandteil eingehängt würde.
        found = {
            HEALTH_COMPONENT_CORE: self._check_core(loop_lag_ms),
            HEALTH_COMPONENT_FRONTEND: self._frontend,
            HEALTH_COMPONENT_DATABASE: self._database,
            HEALTH_COMPONENT_SUPERVISOR: self._supervisor,
            HEALTH_COMPONENT_ADDONS: self._check_addons(addons),
            HEALTH_COMPONENT_INTEGRATIONS: self._check_integrations(unavailable_ratio),
        }
        # Bestandteile, die auf dieser Installation nicht existieren oder deren
        # Prüfung noch nie lief, werden weggelassen — nicht auf None gesetzt.
        components: dict[str, Any] = {
            name: found[name] for name in HEALTH_COMPONENTS if found.get(name) is not None
        }

        result: dict[str, Any] = {
            "loop_lag_ms": round(loop_lag_ms, 1),
            "unavailable_ratio": unavailable_ratio,
            "components": components,
        }
        # Der Selbsttest bekommt keinen eigenen Punkt — er ist kein Bestandteil
        # von Home Assistant, sondern eine Zweitmeinung zum Kern. Sein Ergebnis
        # reist trotzdem mit: es erklärt im Log und im Support-Fall, warum der
        # Kern-Punkt rot (oder eben nur gelb) ist.
        probe = self._ws_probe_fresh()
        if probe is not None:
            result["ws_probe"] = probe
        return result

    @property
    def overall(self) -> str:
        """Schlechtester Zustand über alle bekannten Bestandteile.

        Nur für Log-Ausgaben — die Anzeige leitet ihre Ampel selbst ab, damit
        Plugin und Web-App nicht zwei Wahrheiten pflegen.
        """
        worst = HEALTH_STATUS_OK
        for value in (self._frontend, self._database, self._supervisor):
            if value is not None:
                worst = _worse(worst, value.get("status", HEALTH_STATUS_OK))
        return worst
