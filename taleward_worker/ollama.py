"""Lokale Recaps: Ollama auf diesem PC.

- Läuft schon ein eigenes Ollama (Standardadresse 127.0.0.1:11434), nutzt der Worker dieses.
- Sonst lädt die App Ollama in fester, geprüfter Fassung in ihren Datenordner (kein Administrator nötig) und startet
  es selbst als Unterprozess auf einem eigenen Anschluss, damit es sich mit keinem anderen Ollama in die Quere kommt.
- Das Sprachmodell (nach Grafikkarte, einige GB) lädt der Worker beim ersten Recap über Ollama; danach wird es
  jeweils wieder entladen, damit die Grafikkarte für die Transkription frei ist.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import httpx

from taleward_worker import pfade, prozesse

FASSUNG = "0.35.0"  # gemma4-Modelle brauchen mindestens 0.35.0
QUELLE = f"https://github.com/ollama/ollama/releases/download/v{FASSUNG}"
DATEIEN = {  # Datei und SHA-256 (aus dem Release, von GitHub geprüft)
    "windows": ("ollama-windows-amd64.zip", "d6f7d3dd4f5d013553a78c1e78b2521fcf41d43dd2863e4596cdc046fe6036db"),
    "linux": ("ollama-linux-amd64.tar.zst", "1c114a6b220c5efca2ef2b1e5f01d1e535e26f6cd6d1678c8489325d2835e525"),
}
GROESSE_MB = {"windows": 1394, "linux": 1362}
EIGENER_PORT = 11435
FREMDE_ADRESSE = "http://127.0.0.1:11434"
AUS = "http://127.0.0.1:9"  # Worker ohne Sprachmodell: eine Adresse, unter der sicher nichts antwortet


def _system() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def ordner() -> Path:
    return pfade.basis() / "ollama"


def modellordner() -> Path:
    return pfade.basis() / "ollama-modelle"


def antwortet(url: str, zeit: float = 2.0) -> bool:
    try:
        return httpx.get(f"{url}/api/version", timeout=zeit).status_code == 200
    except httpx.HTTPError:
        return False


def programm() -> Path | None:
    name = "ollama.exe" if sys.platform == "win32" else "ollama"
    wurzel = ordner()
    if not wurzel.exists():
        return None
    for p in (wurzel / name, wurzel / "bin" / name):
        if p.is_file():
            return p
    for p in wurzel.rglob(name):
        if p.is_file():
            return p
    return None


def installiert() -> bool:
    try:
        return (ordner() / "fassung.txt").read_text(encoding="utf-8").strip() == FASSUNG and programm() is not None
    except OSError:
        return False


class Installation:
    """Ollama herunterladen, prüfen und entpacken – im Hintergrund, mit Fortschritt für die Oberfläche."""

    def __init__(self):
        self.phase = "laden"   # laden | entpacken | fertig | fehler | abgebrochen
        self.anteil = 0.0
        self.fehlertext = ""
        self._abbrechen = threading.Event()

    def stand(self) -> dict:
        return {"phase": self.phase, "anteil": round(self.anteil, 3), "fehler": self.fehlertext,
                "erwartetMb": GROESSE_MB[_system()]}

    def abbrechen(self) -> None:
        self._abbrechen.set()

    def starten(self) -> threading.Thread:
        t = threading.Thread(target=self._lauf, daemon=True, name="ollama-installation")
        t.start()
        return t

    def _lauf(self) -> None:
        name, soll = DATEIEN[_system()]
        ziel = pfade.basis() / f"{name}.teil"
        try:
            pfade.basis().mkdir(parents=True, exist_ok=True)
            self._laden(f"{QUELLE}/{name}", ziel, soll)
            self.phase, self.anteil = "entpacken", 0.97
            self._entpacken(ziel, zip_datei=name.endswith(".zip"))
            self.phase, self.anteil = "fertig", 1.0
        except _Abbruch:
            self.phase = "abgebrochen"
        except Exception as e:  # noqa: BLE001 – Text für die Oberfläche
            self.phase, self.fehlertext = "fehler", f"{type(e).__name__}: {e}"[:300]
        finally:
            ziel.unlink(missing_ok=True)

    def _laden(self, url: str, ziel: Path, soll: str) -> None:
        h = hashlib.sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, read=120.0)) as r:
            r.raise_for_status()
            gesamt = int(r.headers.get("content-length") or 0) or GROESSE_MB[_system()] * 2 ** 20
            geladen = 0
            with ziel.open("wb") as f:
                for block in r.iter_bytes(1024 * 512):
                    if self._abbrechen.is_set():
                        raise _Abbruch
                    h.update(block)
                    f.write(block)
                    geladen += len(block)
                    self.anteil = min(0.96, geladen / gesamt * 0.96)
        if h.hexdigest() != soll:
            raise RuntimeError("Prüfsumme von Ollama stimmt nicht – Download verworfen.")

    def _entpacken(self, datei: Path, zip_datei: bool) -> None:
        neu = pfade.basis() / "ollama-neu"
        shutil.rmtree(neu, ignore_errors=True)
        neu.mkdir(parents=True)
        if zip_datei:
            with zipfile.ZipFile(datei) as z:
                _sicher_entpacken_zip(z, neu)
        else:
            import zstandard

            with datei.open("rb") as roh, zstandard.ZstdDecompressor().stream_reader(roh) as strom, \
                    tarfile.open(fileobj=strom, mode="r|") as tar:
                tar.extractall(neu, filter="data")
        (neu / "fassung.txt").write_text(FASSUNG, encoding="utf-8")
        alt = ordner()
        shutil.rmtree(alt, ignore_errors=True)
        neu.rename(alt)
        p = programm()
        if p is None:
            raise RuntimeError("Ollama nicht im Archiv gefunden.")
        if sys.platform != "win32":
            p.chmod(0o755)


class _Abbruch(Exception):
    pass


def _sicher_entpacken_zip(z: zipfile.ZipFile, ziel: Path) -> None:
    wurzel = ziel.resolve()
    for eintrag in z.infolist():
        pfad = (ziel / eintrag.filename).resolve()
        if wurzel not in pfad.parents and pfad != wurzel:
            raise RuntimeError(f"Unzulässiger Pfad im Archiv: {eintrag.filename}")
    z.extractall(ziel)


def protokoll_kuerzen(datei: Path, max_bytes: int = 2_000_000) -> None:
    """ollama.log wächst sonst unbegrenzt (jede Anfrage eine Zeile) – beim Start eine Sicherung behalten."""
    try:
        if datei.exists() and datei.stat().st_size > max_bytes:
            os.replace(datei, datei.with_suffix(".log.1"))
    except OSError:
        pass


class OllamaDienst:
    """Das selbst verwaltete Ollama: starten, prüfen, beenden."""

    def __init__(self):
        self._prozess: subprocess.Popen | None = None
        self._sperre = threading.Lock()
        self.fehler: str = ""  # letzter Startfehler, für die Oberfläche
        atexit.register(self.stoppen)

    @property
    def eigene_adresse(self) -> str:
        return f"http://127.0.0.1:{EIGENER_PORT}"

    def laeuft(self) -> bool:
        return self._prozess is not None and self._prozess.poll() is None

    def adresse(self, an: bool) -> str:
        """Für WORKER_LLM_URL: eigenes/fremdes Ollama oder AUS. Startet das eigene bei Bedarf."""
        if not an:
            return AUS
        if antwortet(FREMDE_ADRESSE):
            return FREMDE_ADRESSE
        if not self.laeuft() and antwortet(self.eigene_adresse):
            # Läuft schon – von einer abgestürzten App übrig geblieben oder von einem zweiten Benutzer. Ein zweiter
            # Start könnte den Port nicht belegen; die vorhandene Instanz tut es genauso.
            self.fehler = ""
            return self.eigene_adresse
        if installiert() and self.starten():
            return self.eigene_adresse
        return AUS

    def starten(self, warten_s: float = 30.0) -> bool:
        with self._sperre:
            if self.laeuft():
                return True
            p = programm()
            if p is None:
                self.fehler = "programm_fehlt"
                return False
            modellordner().mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{EIGENER_PORT}", "OLLAMA_MODELS": str(modellordner()),
                   "OLLAMA_KEEP_ALIVE": "2m", "OLLAMA_MAX_LOADED_MODELS": "1"}
            protokoll_kuerzen(pfade.protokolle() / "ollama.log")
            log = open(pfade.protokolle() / "ollama.log", "ab")
            try:
                self._prozess = subprocess.Popen([str(p), "serve"], stdout=log, stderr=subprocess.STDOUT, env=env,
                                                 cwd=str(p.parent), **prozesse.start_argumente())
            except OSError as e:
                self.fehler = f"start: {e}"
                return False
            finally:
                log.close()
            prozesse.zuordnen(self._prozess)  # Ollama startet Runner-Kinder mit Gigabytes Grafikspeicher
        ende = time.monotonic() + warten_s
        while time.monotonic() < ende:
            if antwortet(self.eigene_adresse, 1.0):
                self.fehler = ""
                return True
            if not self.laeuft():
                code = self._prozess.returncode if self._prozess else None
                self.fehler = f"beendet (Code {code}) – Port {EIGENER_PORT} belegt? Einzelheiten in protokolle/ollama.log"
                return False
            time.sleep(0.5)
        self.fehler = f"antwortet nach {int(warten_s)} s nicht"
        return False

    def stoppen(self) -> None:
        prozesse.beenden(self._prozess, warten=10)
        self._prozess = None

    def entfernen(self) -> None:
        self.stoppen()
        shutil.rmtree(ordner(), ignore_errors=True)
        shutil.rmtree(modellordner(), ignore_errors=True)
