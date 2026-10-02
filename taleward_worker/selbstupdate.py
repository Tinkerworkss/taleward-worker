"""Die Worker-App aktualisiert sich selbst – die Fassung und die Datei kommen vom eigenen Taleward-Server.

Installiert wird nur eine freigegebene Fassung: Der Server reicht die Freigabe (freigabe.txt samt Unterschrift) nur
durch, geprüft wird sie hier mit dem fest eingebauten Schlüssel (taleward_worker/freigabe.py). Repo und Prüfsumme
kommen aus der Freigabe, nicht vom Server.

- Windows: Installer vom eigenen Server laden (nur Pfad /downloads/worker-windows/…, immer an die gekoppelte Adresse),
  SHA-256 aus der Freigabe prüfen, still installieren (/VERYSILENT). Der Installer startet die App danach selbst wieder
  (--hintergrund). Keine Administratorrechte nötig.
- Linux (Installation über install-linux.sh / uv tool): „uv tool install“ mit genau dem freigegebenen Commit, dann
  Neustart.
Installiert wird nur, wenn der Worker gerade nichts verarbeitet.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field

import httpx

from taleward_worker import APP_REPO, VERSION, freigabe, pfade


def neuer(a: str | None, b: str | None) -> bool:
    def tupel(v):
        try:
            return tuple(int(x) for x in (v or "").lstrip("v").split("-")[0].split("."))
        except ValueError:
            return None
    ta, tb = tupel(a), tupel(b)
    if not ta:
        return False
    if not tb:
        return True
    n = max(len(ta), len(tb))
    return ta + (0,) * (n - len(ta)) > tb + (0,) * (n - len(tb))


def system() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def abfragen(server: str, token: str) -> dict | None:
    """Freigegebene Fassung der Worker-App auf dem Server; None = nichts Neueres."""
    try:
        r = httpx.get(f"{server.rstrip('/')}/worker/v1/app-update", params={"system": system()},
                      headers={"Authorization": f"Bearer {token}", "User-Agent": f"TalewardWorker/{VERSION}"},
                      timeout=20)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    return d if neuer(d.get("version"), VERSION) else None


def selbst_installierbar() -> bool:
    """Kann die App sich hier selbst aktualisieren? (gepackte Windows-App bzw. uv-tool-Installation unter Linux)"""
    if sys.platform == "win32":
        return bool(getattr(sys, "frozen", False))
    return "/uv/tools/" in sys.prefix.replace("\\", "/")


@dataclass
class Aktualisierung:
    angebot: dict
    server: str
    token: str
    phase: str = "bereit"   # bereit, laden, installieren, fertig, fehler
    anteil: float = 0.0
    fehler: str = ""
    _faden: threading.Thread | None = field(default=None, repr=False)

    def stand(self) -> dict:
        return {"version": self.angebot.get("version"), "notizen": self.angebot.get("notes"), "phase": self.phase,
                "anteil": round(self.anteil, 3), "fehler": self.fehler, "moeglich": selbst_installierbar()}

    def starten(self, vor_installation, beenden) -> None:
        """vor_installation(): Worker anhalten; beenden(): App schließen (Windows – der Installer startet sie neu)."""
        if self._faden and self._faden.is_alive():
            return
        self._faden = threading.Thread(target=self._lauf, args=(vor_installation, beenden), daemon=True,
                                       name="selbstupdate")
        self._faden.start()

    def _lauf(self, vor_installation, beenden) -> None:
        try:
            if sys.platform == "win32":
                datei = self._laden()
                self.phase = "installieren"
                vor_installation()
                flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                subprocess.Popen([str(datei), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                  "/CLOSEAPPLICATIONS", "/NOCANCEL"], creationflags=flags, close_fds=True)
                self.phase = "fertig"
                beenden()
            else:
                self.phase = "installieren"
                vor_installation()
                self._linux()
                self.phase = "fertig"
                programm = shutil.which("taleward-worker") or os.path.expanduser("~/.local/bin/taleward-worker")
                os.execv(programm, [programm, "--hintergrund"])  # durch die neue Fassung ersetzen
        except Exception as e:  # noqa: BLE001 – Fehler anzeigen, alte Fassung läuft weiter
            self.phase, self.fehler = "fehler", f"{type(e).__name__}: {e}"[:300]

    def _freigabe(self) -> freigabe.Freigabe:
        """Freigabe der angebotenen Fassung prüfen (vom Server durchgereicht, sonst direkt von GitHub)."""
        version = str(self.angebot.get("version") or "")
        if not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version):
            raise RuntimeError("Unbekannte Fassung – Update verworfen.")
        tag = f"worker-v{version}"
        text, sig = self.angebot.get("freigabe"), self.angebot.get("freigabeSignatur")
        try:
            if text and sig:
                return freigabe.lesen(text.encode("utf-8"), sig, APP_REPO, tag)
            return freigabe.von_github(APP_REPO, tag, user_agent=f"TalewardWorker/{VERSION}")
        except freigabe.FreigabeFehler as e:
            raise RuntimeError(f"Keine gültige Freigabe ({e}) – Update verworfen.") from None

    def _adresse(self) -> str:
        """Download nur vom gekoppelten Server: vom Angebot zählt nur der Pfad unter /downloads/worker-windows/."""
        pfad = httpx.URL(str(self.angebot.get("url") or "")).path
        if not re.fullmatch(r"/downloads/worker-windows/[0-9.]+/[A-Za-z0-9._-]+", pfad):
            raise RuntimeError("Der Server nennt keine gültige Datei.")
        return self.server.rstrip("/") + pfad

    def _laden(self):
        f = self._freigabe()
        soll = f.dateien.get("TalewardWorker-Setup.exe")
        if not soll:
            raise RuntimeError("Der Installer ist nicht freigegeben – Update verworfen.")
        url = self._adresse()
        ordner = pfade.basis() / "updates"
        shutil.rmtree(ordner, ignore_errors=True)
        ordner.mkdir(parents=True)
        ziel = ordner / f"TalewardWorker-Setup-{self.angebot['version']}.exe"
        self.phase = "laden"
        h = hashlib.sha256()
        with httpx.stream("GET", url, headers={"Authorization": f"Bearer {self.token}"}, timeout=httpx.Timeout(30, read=300),
                          follow_redirects=False) as r:
            r.raise_for_status()
            gesamt = int(r.headers.get("content-length") or self.angebot.get("sizeBytes") or 0)
            geladen = 0
            with ziel.open("wb") as datei:
                for block in r.iter_bytes(1024 * 256):
                    h.update(block)
                    datei.write(block)
                    geladen += len(block)
                    if gesamt:
                        self.anteil = min(0.99, geladen / gesamt)
        if h.hexdigest() != soll:
            ziel.unlink(missing_ok=True)
            raise RuntimeError("Prüfsumme stimmt nicht mit der Freigabe überein – Update verworfen.")
        return ziel

    def _linux(self) -> None:
        f = self._freigabe()  # Repo fest, Commit aus der Freigabe – Angaben des Servers zu Repo/Tag zählen nicht
        uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
        quelle = f"taleward-worker[qt] @ https://github.com/{APP_REPO}/archive/{f.commit}.zip"
        erg = subprocess.run([uv, "tool", "install", "--force", "--python", "3.12", quelle],
                             capture_output=True, text=True, timeout=1800)
        if erg.returncode != 0:
            raise RuntimeError((erg.stderr or erg.stdout)[-300:])
