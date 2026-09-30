"""Fenster (pywebview) und Symbol im Infobereich (pystray). Die Oberfläche selbst ist HTML (ui/), sie ruft die
Methoden von ``Api`` auf und fragt den Zustand zweimal pro Sekunde ab."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser

from taleward_worker import VERSION, autostart, hardware, motor, ollama, pfade, selbstupdate, server
from taleward_worker.dienst import Dienst
from taleward_worker.einstellungen import Einstellungen

log = logging.getLogger("taleward_worker")
UPDATE_PRUEFEN_ALLE = 6 * 3600


class Api:
    """Alles, was die Oberfläche aufrufen darf. Rückgaben sind JSON-fähig."""

    def __init__(self, app: "WorkerApp"):
        self._app = app

    # -- Zustand
    def zustand(self) -> dict:
        a = self._app
        inst = motor.installiert()
        return {
            "version": VERSION, "system": "windows" if sys.platform == "win32" else "linux",
            "einstellungen": a.e.fuer_oberflaeche(), "autostart": autostart.aktiv(),
            "dienst": a.dienst.fuer_oberflaeche(), "motor": inst, "profil": a.dienst.profil(),
            "installation": a.installation.stand() if a.installation else None,
            "hardware": a.hardware, "serverVersion": a.server_version, "serverName": a.server_name,
            "appUpdate": a.app_update.stand() if a.app_update else None, "trayVorhanden": a.tray is not None,
            "entwicklung": bool(os.environ.get("TALEWARD_MOTOR")),
            "ollama": a.ollama_stand(),
        }

    def hardware_pruefen(self) -> dict:
        self._app.hardware = hardware.bericht()
        return self._app.hardware

    # -- Einrichtung
    def server_pruefen(self, adresse: str) -> dict:
        try:
            a = server.adresse_normalisieren(adresse)
            info = server.pruefen(a)
        except server.ServerFehler as e:
            return {"ok": False, "fehler": e.code}
        return {"ok": True, "adresse": a, "name": info["name"], "unverschluesselt": server.unverschluesselt(a)}

    def koppeln(self, adresse: str, code: str, name: str, unsicher: bool = False) -> dict:
        a = self._app
        try:
            adr = server.adresse_normalisieren(adresse)
            if server.unverschluesselt(adr) and not unsicher:
                return {"ok": False, "fehler": "unverschluesselt"}
            d = server.koppeln(adr, code, name)
        except server.ServerFehler as e:
            return {"ok": False, "fehler": e.code, "text": e.text}
        a.dienst.stoppen()
        a.e.server, a.e.token, a.e.name, a.e.unsicher = adr, d["token"], d.get("name") or name, bool(unsicher)
        a.e.speichern()
        a.server_version = d.get("serverVersion") or a.server_version
        a.fassung_abfragen()
        return {"ok": True, "name": a.e.name, "serverVersion": a.server_version}

    def installieren(self, testmodus: bool = False, prozessor: bool | None = None) -> dict:
        if prozessor is not None:
            self._app.e.geraet = "cpu" if prozessor else "auto"
            self._app.e.speichern()
        self._app.installieren(bool(testmodus))
        return {"ok": True}

    def profil_vorschau(self, grenze_mb: int, modell: str | None = None, prozessor: bool | None = None) -> dict:
        """Was eine Einstellung bewirken würde (für den Schieberegler), ohne sie zu speichern."""
        a = self._app
        karten = hardware.grafikkarten()
        return hardware.profil(karten[0].vram_mb if karten else None, int(grenze_mb or 0), modell or a.e.modell,
                               prozessor=(a.e.geraet == "cpu") if prozessor is None else bool(prozessor))

    def installation_abbrechen(self) -> None:
        if self._app.installation:
            self._app.installation.abbrechen()

    # -- Betrieb
    def starten(self) -> None:
        self._app.dienst.starten()

    def stoppen(self) -> None:
        threading.Thread(target=self._app.dienst.stoppen, daemon=True).start()

    def pausieren(self, an: bool) -> None:
        self._app.dienst.pausieren(bool(an))
        self._app.tray_aktualisieren()

    def einstellung(self, name: str, wert) -> dict:
        a = self._app
        if name == "autostart":
            try:
                autostart.setzen(bool(wert))
            except OSError as e:
                return {"ok": False, "fehler": str(e)}
            a.e.autostart = bool(wert)
        elif name == "im_hintergrund":
            a.e.im_hintergrund = bool(wert)
        elif name == "auto_update":
            a.e.auto_update = bool(wert)
        elif name == "recaps_lokal":
            a.e.recaps_lokal = bool(wert)
            a.e.speichern()
            if a.e.recaps_lokal and not ollama.installiert() and not ollama.antwortet(ollama.FREMDE_ADRESSE):
                a.ollama_installieren()
            else:
                a.dienst.neu_starten_wenn_frei()
        elif name == "modell" and wert in ("auto", "large-v3", "large-v3-turbo"):
            a.e.modell = wert
            a.e.speichern()
            a.dienst.neu_starten_wenn_frei()
        elif name == "vram_grenze_mb":
            a.e.vram_grenze_mb = max(0, int(wert or 0))
            a.e.speichern()
            a.dienst.neu_starten_wenn_frei()
        elif name == "geraet" and wert in ("auto", "cpu"):
            a.e.geraet = wert
            a.e.speichern()
            if not a.motor_passt():  # KI-Paket nur für den Prozessor installiert → Grafikkarten-Fassung laden
                a.installieren(a.e.testmodus)
            else:
                a.dienst.neu_starten_wenn_frei()
        elif name == "sprache" and wert in ("", "de", "en"):
            a.e.sprache = wert
        elif name == "hinweis_hintergrund_gezeigt":
            a.e.hinweis_hintergrund_gezeigt = True
        else:
            return {"ok": False, "fehler": "unbekannt"}
        a.e.speichern()
        return {"ok": True}

    def app_aktualisieren(self) -> dict:
        a = self._app
        if not a.app_update:
            return {"ok": False}
        a.selbst_aktualisieren()
        return {"ok": True}

    def entkoppeln(self) -> None:
        a = self._app
        a.dienst.stoppen()
        a.e.token = ""
        a.e.speichern()

    def ki_entfernen(self, modelle_auch: bool = True) -> None:
        self._app.dienst.stoppen()
        motor.entfernen(modelle_auch=bool(modelle_auch))
        self._app.e.motor_fassung = ""
        self._app.e.speichern()

    def ollama_entfernen(self) -> None:
        a = self._app
        a.e.recaps_lokal = False
        a.e.speichern()
        if a.ollama_installation:
            a.ollama_installation.abbrechen()
        a.dienst.ollama.entfernen()
        a.dienst.neu_starten_wenn_frei()

    def speicherbelegung(self) -> dict:
        def mb(p):
            return pfade.ordnergroesse(p) // 2 ** 20 if p.exists() else 0
        return {"motorMb": mb(pfade.motor()) + mb(pfade.basis() / "python"), "modelleMb": mb(pfade.modelle()),
                "ollamaMb": mb(ollama.ordner()) + mb(ollama.modellordner()),
                "ordner": str(pfade.basis())}

    def protokoll(self) -> list[str]:
        z = list(self._app.dienst.protokoll.zeilen)
        inst = self._app.installation
        # Die Ausgabe der Installation nur, solange sie läuft oder wenn sie gescheitert ist – sonst stünde sie für
        # immer unter dem Protokoll des Workers, und dessen neue Zeilen wären nicht mehr unten zu sehen
        if inst and inst.phase != "fertig":
            z += ["", "— Installation —", *inst.zeilen[-200:]]
        return z[-800:]

    def ordner_oeffnen(self) -> None:
        ziel = str(pfade.basis())
        if sys.platform == "win32":
            os.startfile(ziel)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", ziel], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def link_oeffnen(self, url: str) -> None:
        if url.startswith("https://"):
            webbrowser.open(url)

    def verwaltung_oeffnen(self) -> None:
        if self._app.e.server:
            webbrowser.open(self._app.e.server.rstrip("/") + "/verwaltung/worker")

    def fenster_verstecken(self) -> None:
        self._app.verstecken()

    def beenden(self) -> None:
        threading.Thread(target=self._app.beenden, daemon=True).start()


class WorkerApp:
    def __init__(self, hintergrund: bool = False):
        self.e = Einstellungen.laden()
        self.dienst = Dienst(self.e)
        self.installation: motor.Installation | None = None
        self.ollama_installation: ollama.Installation | None = None
        self._fremd_geprueft: tuple[float, bool] = (0.0, False)
        self.hardware: dict | None = None
        self.server_version: str | None = None
        self.server_name: str | None = None
        self.app_update: selbstupdate.Aktualisierung | None = None
        self.hintergrund = hintergrund
        self.fenster = None
        self.tray = None
        self._beendet = threading.Event()

    # ------------------------------------------------------------------ Ablauf
    def fassung_abfragen(self) -> None:
        if not self.e.gekoppelt:
            return
        try:
            self.server_version = server.konfiguration(self.e.server, self.e.token).get("serverVersion")
            self.server_name = server.pruefen(self.e.server).get("name")
        except server.ServerFehler as e:
            if e.code == "abgelehnt":
                self.dienst._setzen("abgelehnt")

    def motor_passt(self) -> bool:
        inst = motor.installiert()
        if os.environ.get("TALEWARD_MOTOR"):
            return True
        if not inst:
            return False
        if bool(inst.get("testmodus")) != self.e.testmodus:
            return False
        if not self.e.testmodus and self.e.geraet != "cpu" and inst.get("backend") == "cpu":
            return False  # Grafikkarte gewünscht, installiert ist nur die Prozessor-Fassung
        if not self.e.testmodus and self.e.geraet != "cpu" and inst.get("torch", {}).get("cuda", "?") is None:
            return False  # als Grafikkarten-Fassung eingetragen, aber PyTorch kam ohne CUDA an → neu installieren
        return not self.server_version or inst.get("fassung") == self.server_version

    # -- Ollama (lokale Recaps)
    def ollama_installieren(self) -> None:
        if self.ollama_installation and self.ollama_installation.phase in ("laden", "entpacken"):
            return
        self.ollama_installation = ollama.Installation()
        faden = self.ollama_installation.starten()

        def danach():
            faden.join()
            if self.ollama_installation.phase == "fertig":
                self.dienst.neu_starten_wenn_frei()
        threading.Thread(target=danach, daemon=True).start()

    def ollama_stand(self) -> dict:
        jetzt = time.monotonic()
        if jetzt - self._fremd_geprueft[0] > 10:  # die Oberfläche fragt oft – das fremde Ollama selten prüfen
            self._fremd_geprueft = (jetzt, ollama.antwortet(ollama.FREMDE_ADRESSE, 0.5)
                                    if not self.dienst.ollama.laeuft() else False)
        inst = self.ollama_installation
        return {"an": self.e.recaps_lokal, "installiert": ollama.installiert(), "laeuft": self.dienst.ollama.laeuft(),
                "fremd": self._fremd_geprueft[1], "fassung": ollama.FASSUNG, "fehler": self.dienst.ollama.fehler,
                "moeglich": bool((self.hardware or {}).get("recapsMoeglich", True)),
                "installation": inst.stand() if inst and inst.phase != "fertig" else None,
                "sprachmodell": self.dienst.info.get("llm")}

    def installieren(self, testmodus: bool) -> None:
        if self.installation and self.installation.phase not in ("fertig", "fehler", "abgebrochen"):
            return
        self.e.testmodus = testmodus
        self.e.speichern()
        fehlt_gb = motor.platz_fehlt_gb(testmodus, "cpu" if self.e.geraet == "cpu" else "cuda")
        if fehlt_gb:
            # Gar nicht erst anfangen: ein voller Datenträger bricht mittendrin ab, und Windows selbst leidet
            self.installation = motor.Installation(self.server_version or "", testmodus=testmodus)
            self.installation.phase, self.installation.fehler = "fehler", "kein_platz"
            self.installation.fehlertext = f"Es fehlen etwa {fehlt_gb:.0f} GB freier Speicherplatz."
            self.dienst.protokoll.schreiben(f"Installation nicht gestartet: {self.installation.fehlertext}")
            return
        war_aktiv = {"an": False}

        def vor():
            war_aktiv["an"] = self.dienst.laeuft()
            self.dienst.stoppen()

        def nach():
            if self.installation and self.installation.phase != "fehler":
                self.e.motor_fassung = self.installation.fassung
                self.e.motor_art = "test" if testmodus else "ki"
                self.e.speichern()
            if self.e.gekoppelt:
                self.dienst.starten()

        backend = "cpu" if self.e.geraet == "cpu" else "cuda"
        self.installation = motor.Installation(self.server_version or "", testmodus=testmodus, backend=backend,
                                               vor_tausch=vor, nach_tausch=nach)
        self.installation.starten()

    def _motor_pruefen(self, erster: bool) -> None:
        """Serverfassung abfragen; passt das KI-Paket nicht mehr, im Hintergrund nachziehen (nur ohne Auftrag)."""
        self.fassung_abfragen()
        if not self.e.gekoppelt:
            return
        if self.motor_passt():
            if erster and not self.dienst.laeuft():
                self.dienst.starten()
        elif motor.installiert() or os.environ.get("TALEWARD_MOTOR"):
            # Neue Serverfassung: Motor im Hintergrund aktualisieren (alter läuft bis zum Tausch weiter)
            if erster and not self.dienst.laeuft():
                self.dienst.starten()
            laeuft_installation = self.installation and self.installation.phase not in ("fertig", "fehler",
                                                                                         "abgebrochen")
            if self.dienst.zustand.get("art") != "arbeitet" and not laeuft_installation:
                self.installieren(self.e.testmodus)

    def _ollama_pruefen(self) -> None:
        """Lokale Recaps gewünscht, aber Ollama fehlt oder hat eine alte Fassung (nach einem App-Update)? Dann
        nachladen – sonst wanderten die Recaps still auf den Server, und die Oberfläche zeigte nichts."""
        if not self.e.recaps_lokal or not self.e.gekoppelt or self.e.testmodus:
            return
        if ollama.installiert() or ollama.antwortet(ollama.FREMDE_ADRESSE):
            return
        if self.ollama_installation and self.ollama_installation.phase not in ("fertig", "fehler"):
            return
        if self.ollama_installation and self.ollama_installation.phase == "fehler":
            return  # nicht im Kreis laden – der Nutzer sieht den Fehler und kann neu anstoßen
        self.dienst.protokoll.schreiben("Ollama fehlt oder ist veraltet – wird geladen")
        self.ollama_installieren()

    def _hintergrund_pflege(self) -> None:
        """Beim Start: Fassung prüfen, ggf. Motor aktualisieren, Worker starten. Danach alle 5 Minuten die
        Serverfassung (ein kleiner Abruf – so zieht das KI-Paket kurz nach einem Server-Update nach) und alle
        6 Stunden, ob es eine neue App gibt."""
        naechste_app_pruefung = 0.0
        erster = True
        while not self._beendet.is_set():
            try:
                self._motor_pruefen(erster)
                self._ollama_pruefen()
                if time.time() >= naechste_app_pruefung and self.e.gekoppelt:
                    self.app_update_pruefen()
                    naechste_app_pruefung = time.time() + UPDATE_PRUEFEN_ALLE
                erster = False
                self.tray_aktualisieren()
            except Exception as e:  # nie sterben – sonst startet der Worker nie und niemand erfährt warum
                self.dienst.protokoll.schreiben(f"Fehler in der Hintergrundpflege: {type(e).__name__}: {e}")
                log.exception("Hintergrundpflege")
            if self._beendet.wait(300):
                return
            try:
                self.app_update_wenn_frei()
            except Exception as e:
                self.dienst.protokoll.schreiben(f"Fehler beim App-Update: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ Selbst-Update der App
    def app_update_pruefen(self) -> None:
        angebot = selbstupdate.abfragen(self.e.server, self.e.token)
        if angebot is None:
            if self.app_update and self.app_update.phase in ("bereit", "fehler"):
                self.app_update = None
            return
        if not self.app_update or self.app_update.angebot.get("version") != angebot.get("version"):
            self.app_update = selbstupdate.Aktualisierung(angebot, self.e.server, self.e.token)
        self.app_update_wenn_frei()

    def app_update_wenn_frei(self) -> None:
        """Automatisch nur, wenn gerade kein Auftrag läuft und das KI-Paket nicht installiert wird."""
        u = self.app_update
        if not u or u.phase != "bereit" or not self.e.auto_update or not selbstupdate.selbst_installierbar():
            return
        beschaeftigt = self.dienst.zustand.get("art") == "arbeitet" or (
            self.installation and self.installation.phase not in ("fertig", "fehler", "abgebrochen"))
        if not beschaeftigt:
            self.selbst_aktualisieren()

    def selbst_aktualisieren(self) -> None:
        if self.app_update and self.app_update.phase in ("bereit", "fehler"):
            self.app_update.phase, self.app_update.fehler = "bereit", ""
            self.app_update.starten(self.dienst.stoppen, self.beenden)

    # ------------------------------------------------------------------ Fenster und Infobereich
    def zeigen(self) -> None:
        if self.fenster:
            self.fenster.show()
            self.fenster.restore()

    def verstecken(self) -> None:
        if self.fenster:
            if self.tray is not None:
                self.fenster.hide()
                if not self.e.hinweis_hintergrund_gezeigt:
                    self.e.hinweis_hintergrund_gezeigt = True
                    self.e.speichern()
                    en = self.e.sprache.startswith("en")
                    try:
                        self.tray.notify("The worker keeps running in the background. Right-click the icon to quit."
                                         if en else "Der Worker läuft im Hintergrund weiter. Beenden über "
                                         "Rechtsklick auf das Symbol.", "Taleward Worker")
                    except Exception:  # noqa: BLE001 – Hinweis ist Beiwerk
                        pass
            else:
                self.fenster.minimize()

    def _abmeldung_beachten(self) -> None:
        """Windows: Beim Herunterfahren oder Abmelden nicht im Weg stehen. Das Fenster lehnt Schließen sonst ab
        (es soll ja in den Infobereich wandern) – Windows zeigte dann „Taleward Worker verhindert das
        Herunterfahren“. SessionEnding kommt vor der Schließanfrage; danach lässt _beim_schliessen sie durch."""
        if sys.platform != "win32":
            return
        try:
            import clr  # pythonnet, kommt mit pywebview

            clr.AddReference("System")
            from Microsoft.Win32 import SystemEvents  # type: ignore[import-not-found]

            def ende(_sender, _args):
                self._beendet.set()
                threading.Thread(target=self.beenden, daemon=True, name="abmeldung").start()

            SystemEvents.SessionEnding += ende
        except Exception as e:  # noqa: BLE001 – dann bleibt es beim alten Verhalten
            log.debug("SessionEnding nicht abonniert: %s", e)

    def beenden(self) -> None:
        self._beendet.set()
        for inst in (self.installation, self.ollama_installation):
            if inst is not None:
                inst.abbrechen()  # sonst schreibt uv nach dem Ende der App minutenlang weiter in motor-neu
        self.dienst.stoppen()
        self.dienst.ollama.stoppen()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.fenster:
            self.fenster.destroy()

    def _beim_schliessen(self):
        if self._beendet.is_set():
            return True
        if self.e.im_hintergrund and self.e.gekoppelt:
            self.verstecken()
            return False
        threading.Thread(target=self.beenden, daemon=True).start()
        return False

    def tray_aktualisieren(self) -> None:
        if self.tray is None:
            return
        try:
            from taleward_worker.symbole import symbol

            art = self.dienst.zustand.get("art", "gestoppt")
            self.tray.icon = symbol(art)
            self.tray.title = tray_titel(self.dienst.fuer_oberflaeche(), self.e.sprache)
            self.tray.update_menu()
        except Exception:  # noqa: BLE001 – Infobereich ist Beiwerk
            log.debug("Infobereich nicht aktualisiert", exc_info=True)

    def _tray_starten(self) -> None:
        try:
            import pystray

            from taleward_worker.symbole import symbol
        except Exception:  # noqa: BLE001
            return
        de = not self.e.sprache.startswith("en")

        def pause_text(_):
            return (("Fortsetzen" if de else "Resume") if self.e.pausiert else ("Pausieren" if de else "Pause"))

        menue = pystray.Menu(
            pystray.MenuItem("Öffnen" if de else "Open", lambda: self.zeigen(), default=True),
            pystray.MenuItem(pause_text, lambda: (self.dienst.pausieren(not self.e.pausiert),
                                                  self.tray_aktualisieren())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden" if de else "Quit", lambda: threading.Thread(target=self.beenden,
                                                                                   daemon=True).start()),
        )
        try:
            icon = pystray.Icon("taleward-worker", symbol("gestoppt"), "Taleward Worker", menue)
            icon.run_detached()
        except Exception:  # noqa: BLE001 – z. B. Wayland ohne Infobereich
            return
        if sys.platform != "win32":
            # X11 ohne Infobereich (z. B. GNOME ohne Erweiterung): pystray „läuft“, das Symbol ist aber unsichtbar.
            # Dann lieber kein Symbol – sonst wäre ein verstecktes Fenster nicht mehr erreichbar.
            time.sleep(1.0)
            if hasattr(icon, "_systray_manager") and not icon._systray_manager:
                try:
                    icon.stop()
                except Exception:  # noqa: BLE001
                    pass
                return
        self.tray = icon

    def _tray_nachfuehren(self) -> None:
        letzte = None
        while not self._beendet.wait(2):
            jetzt = (self.dienst.zustand.get("art"), self.e.pausiert, round(self.dienst.zustand.get("p") or 0, 1))
            if jetzt != letzte:
                letzte = jetzt
                self.tray_aktualisieren()

    def run(self) -> None:
        import webview

        from taleward_worker import einzeln

        if einzeln.andere_wecken() or not einzeln.sperren():
            return
        einzeln.lauschen(self.zeigen, self.beenden)
        self._abmeldung_beachten()
        if not webview2_vorhanden():
            webview2_fehlt_melden()
            return
        self.hardware = hardware.bericht()
        self._tray_starten()
        versteckt = self.hintergrund and self.e.gekoppelt
        breite, hoehe, mindestens = fenstergroesse(bildschirm_hoehe())
        self.fenster = webview.create_window(
            "Taleward Worker", str(pfade.ressourcen() / "ui" / "index.html"), js_api=Api(self),
            width=breite, height=hoehe, min_size=mindestens, background_color="#F2E8D5",
            hidden=versteckt and self.tray is not None, minimized=versteckt and self.tray is None)
        self.fenster.events.closing += self._beim_schliessen
        threading.Thread(target=self._hintergrund_pflege, daemon=True, name="pflege").start()
        threading.Thread(target=self._tray_nachfuehren, daemon=True, name="tray").start()
        webview.start(debug=bool(os.environ.get("TALEWARD_DEBUG")), private_mode=True)
        self._beendet.set()
        self.dienst.stoppen()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass


def tray_titel(d: dict, sprache: str) -> str:
    en = sprache.startswith("en")
    z = d["zustand"]
    art = z.get("art")
    texte = {
        "warte": ("Bereit – wartet auf Aufnahmen", "Ready – waiting for recordings"),
        "arbeitet": (f"Arbeitet … {round((z.get('p') or 0) * 100)} %", f"Working … {round((z.get('p') or 0) * 100)}%"),
        "pausiert": ("Pausiert", "Paused"),
        "server_pausiert": ("Auf dem Server pausiert", "Paused on the server"),
        "getrennt": ("Keine Verbindung zum Server", "No connection to the server"),
        "startet": ("Startet …", "Starting …"),
        "fehler": ("Braucht deine Hilfe", "Needs your attention"),
        "abgelehnt": ("Nicht mehr gekoppelt", "No longer paired"),
        "abgestuerzt": ("Startet neu …", "Restarting …"),
        "gestoppt": ("Angehalten", "Stopped"),
    }
    de_text, en_text = texte.get(art, ("Taleward Worker", "Taleward Worker"))
    return "Taleward Worker – " + (en_text if en else de_text)


# ---------------------------------------------------------------------- Fenster und Browser-Kern
FENSTER = (560, 760)
FENSTER_MINDESTENS = (460, 600)
WEBVIEW2_LINK = "https://developer.microsoft.com/microsoft-edge/webview2/"


def bildschirm_hoehe() -> int | None:
    try:
        import webview

        return int(webview.screens[0].height) if webview.screens else None
    except Exception:  # noqa: BLE001 – dann die Vorgabe
        return None


def fenstergroesse(bildschirm: int | None) -> tuple[int, int, tuple[int, int]]:
    """Fenstergröße so, dass es auch auf 1366×768 bei 125 % Skalierung (≈ 614 logische Pixel) ganz sichtbar ist –
    die Knöpfe „Weiter“ und „Installieren“ sitzen unten."""
    breite, hoehe = FENSTER
    if not bildschirm:
        return breite, hoehe, FENSTER_MINDESTENS
    nutzbar = max(400, bildschirm - 80)  # Taskleiste und Fensterrahmen
    return breite, min(hoehe, nutzbar), (FENSTER_MINDESTENS[0], min(FENSTER_MINDESTENS[1], nutzbar))


def webview2_vorhanden() -> bool:
    """Windows: pywebview fiele ohne WebView2 still auf den alten Internet-Explorer-Kern zurück, der unsere
    Oberfläche nicht ausführen kann – das Fenster bliebe leer, ohne jede Meldung."""
    if sys.platform != "win32":
        return True
    try:
        from webview.platforms import winforms

        return bool(getattr(winforms, "is_chromium", True))
    except Exception:  # noqa: BLE001 – lieber starten als fälschlich blockieren
        return True


def webview2_fehlt_melden() -> None:
    log.error("WebView2 fehlt – Oberfläche kann nicht starten")
    if sys.platform != "win32":
        return
    try:
        import ctypes

        antwort = ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None, "Taleward Worker braucht die Microsoft-Komponente „WebView2“, die auf diesem PC fehlt.\n\n"
            "Jetzt die Download-Seite öffnen? (Installieren, dann Taleward Worker erneut starten.)",
            "Taleward Worker", 0x14)  # MB_YESNO | MB_ICONERROR
        if antwort == 6:  # IDYES
            webbrowser.open(WEBVIEW2_LINK)
    except Exception:  # noqa: BLE001
        pass
