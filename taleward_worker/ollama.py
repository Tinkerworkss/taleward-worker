"""Lokale Recaps: Ollama auf diesem PC.

- Läuft schon ein eigenes Ollama (Standardadresse 127.0.0.1:11434), nutzt der Worker dieses.
- Sonst lädt die App Ollama in fester, geprüfter Fassung in ihren Datenordner (kein Administrator nötig) und startet
  es selbst als Unterprozess auf einem eigenen Anschluss, damit es sich mit keinem anderen Ollama in die Quere kommt.
- Das Sprachmodell lädt der Worker beim ersten Recap über Ollama (etwa 5 GB); danach wird es jeweils wieder
  entladen, damit die Grafikkarte für die Transkription frei ist.
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

from taleward_worker import pfade

FASSUNG = "0.34.4"
QUELLE = f"https://github.com/ollama/ollama/releases/download/v{FASSUNG}"
DATEIEN = {  # Datei und SHA-256 (aus dem Release, von GitHub geprüft)
    "windows": ("ollama-windows-amd64.zip", "535193f38f3344e5b08f5d1c171c31ce11aa17f0124ff69ae26d8ec7fe06fa62"),
    "linux": ("ollama-linux-amd64.tar.zst", "c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533"),
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


class OllamaDienst:
    """Das selbst verwaltete Ollama: starten, prüfen, beenden."""

    def __init__(self):
        self._prozess: subprocess.Popen | None = None
        self._sperre = threading.Lock()
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
        if installiert() and self.starten():
            return self.eigene_adresse
        return AUS

    def starten(self, warten_s: float = 30.0) -> bool:
        with self._sperre:
            if self.laeuft():
                return True
            p = programm()
            if p is None:
                return False
            modellordner().mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{EIGENER_PORT}", "OLLAMA_MODELS": str(modellordner()),
                   "OLLAMA_KEEP_ALIVE": "2m", "OLLAMA_MAX_LOADED_MODELS": "1"}
            log = open(pfade.protokolle() / "ollama.log", "ab")
            extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
            try:
                self._prozess = subprocess.Popen([str(p), "serve"], stdout=log, stderr=subprocess.STDOUT, env=env,
                                                 cwd=str(p.parent), **extra)
            except OSError:
                return False
            finally:
                log.close()
        ende = time.monotonic() + warten_s
        while time.monotonic() < ende:
            if antwortet(self.eigene_adresse, 1.0):
                return True
            if not self.laeuft():
                return False
            time.sleep(0.5)
        return False

    def stoppen(self) -> None:
        p = self._prozess
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        self._prozess = None

    def entfernen(self) -> None:
        self.stoppen()
        shutil.rmtree(ordner(), ignore_errors=True)
        shutil.rmtree(modellordner(), ignore_errors=True)
